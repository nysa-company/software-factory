#!/usr/bin/env python3
"""Focused non-agent controller persistence tests."""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import copy
from contextlib import redirect_stdout
import hashlib
import hmac
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "factory_controller", ROOT / "scripts/factory-controller.py"
)
assert SPEC and SPEC.loader
CONTROL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTROL)

REPORTER_SPEC = importlib.util.spec_from_file_location(
    "factory_incident_reporter", ROOT / "scripts/factory-incident-reporter.py"
)
assert REPORTER_SPEC and REPORTER_SPEC.loader
REPORTER = importlib.util.module_from_spec(REPORTER_SPEC)
REPORTER_SPEC.loader.exec_module(REPORTER)

PASSPORT_SPEC = importlib.util.spec_from_file_location(
    "ticket_passport_for_controller_test", ROOT / "scripts/ticket-passport.py"
)
assert PASSPORT_SPEC and PASSPORT_SPEC.loader
PASSPORT = importlib.util.module_from_spec(PASSPORT_SPEC)
PASSPORT_SPEC.loader.exec_module(PASSPORT)

STATE_SPEC = importlib.util.spec_from_file_location(
    "state_machine_for_controller_test", ROOT / "scripts/state-machine.py"
)
assert STATE_SPEC and STATE_SPEC.loader
STATE = importlib.util.module_from_spec(STATE_SPEC)
STATE_SPEC.loader.exec_module(STATE)

ATTEST_SPEC = importlib.util.spec_from_file_location(
    "ticket_attest_for_controller_test", ROOT / "scripts/ticket-attest.py"
)
assert ATTEST_SPEC and ATTEST_SPEC.loader
ATTEST = importlib.util.module_from_spec(ATTEST_SPEC)
ATTEST_SPEC.loader.exec_module(ATTEST)
ROUTER_SPEC = importlib.util.spec_from_file_location(
    "controller_test_router", ROOT / "scripts/model-router.py"
)
assert ROUTER_SPEC and ROUTER_SPEC.loader
ROUTER = importlib.util.module_from_spec(ROUTER_SPEC)
ROUTER_SPEC.loader.exec_module(ROUTER)
MANAGER_SPEC = importlib.util.spec_from_file_location(
    "controller_test_manager", ROOT / "scripts/model-manager.py"
)
assert MANAGER_SPEC and MANAGER_SPEC.loader
MANAGER = importlib.util.module_from_spec(MANAGER_SPEC)
MANAGER_SPEC.loader.exec_module(MANAGER)
HANDOFF_SPEC = importlib.util.spec_from_file_location(
    "controller_test_handoff", ROOT / "scripts/lib/failed_attempt_handoff.py"
)
assert HANDOFF_SPEC and HANDOFF_SPEC.loader
HANDOFF = importlib.util.module_from_spec(HANDOFF_SPEC)
HANDOFF_SPEC.loader.exec_module(HANDOFF)
FACTORY_ISSUE = "https://github.com/nysa-company/software-factory/issues/253"


def state_transition(
    stage: str, receipt: str = "b" * 64, ticket: str = "T-110"
) -> dict:
    return {
        "action": stage.partition(" ")[0],
        "detail": stage.partition(" ")[2] or None,
        "loop": None,
        "receipt": receipt,
        "role": STATE.stage_role(stage),
        "schema": "nysa.software-factory.state-machine/v1",
        "stage": stage,
        "status": "ok",
        "ticket": ticket,
    }


class FactoryControllerTest(unittest.TestCase):
    def test_native_launch_agent_runs_without_the_retired_runtime(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("native LaunchAgent smoke requires macOS")
        launchctl = Path("/bin/launchctl")
        uid = os.getuid()
        prefix = [str(launchctl), "asuser", str(uid), str(launchctl)]
        domain = f"gui/{uid}"
        if subprocess.run(
            prefix + ["print", domain], capture_output=True, check=False,
        ).returncode:
            self.skipTest("interactive launchd domain is unavailable")
        with tempfile.TemporaryDirectory(prefix="factory-native-controller.") as raw:
            root = Path(raw)
            home = root / "home"
            product = root / "product"
            marker = root / "reconciled"
            sentinel = root / "retired-runtime-invoked"
            project = f"smoke-{os.getpid()}"
            removed = "her" + "mes"
            binary = home / ".factory/bin"
            logs = home / ".factory/logs"
            jobs = home / "Library/LaunchAgents"
            for directory in (binary, logs, jobs, product / "factory/runs"):
                directory.mkdir(parents=True, exist_ok=True)
            retired_command = binary / removed
            retired_command.write_text(
                f"#!/bin/sh\n: > {sentinel!s}\nexit 97\n", encoding="utf-8",
            )
            retired_command.chmod(0o700)
            launcher = binary / "factory-launch"
            launcher.write_text(
                "#!/bin/sh\nset -eu\n"
                f"test \"$1\" = {project!s}\n"
                "test \"$2\" = reconcile\n"
                "test \"$3\" = --json\n"
                f"test ! -e {home!s}/.{removed}\n"
                f": > {marker!s}\n"
                "printf '%s\\n' '{}'\n",
                encoding="utf-8",
            )
            launcher.chmod(0o700)
            template = (
                ROOT / "scripts/launchd/com.factory.controller.plist.template"
            ).read_text(encoding="utf-8")
            path = jobs / f"com.factory.controller.{project}.plist"
            path.write_text(
                template.replace("__HOME__", str(home))
                .replace("__PROJECT_SLUG__", project)
                .replace("__PRODUCT_ROOT__", str(product)),
                encoding="utf-8",
            )
            service = f"{domain}/com.factory.controller.{project}"
            try:
                result = subprocess.run(
                    prefix + ["bootstrap", domain, str(path)],
                    capture_output=True, check=False, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                deadline = time.monotonic() + 15
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(marker.exists(), "native controller did not reconcile")
                self.assertFalse(sentinel.exists(), "retired runtime command was invoked")
                self.assertFalse((home / f".{removed}").exists())
            finally:
                subprocess.run(
                    prefix + ["bootout", service], capture_output=True,
                    check=False, timeout=30,
                )

    def test_launch_agent_does_not_throttle_bounded_provider_probes(self) -> None:
        template = ROOT / "scripts/launchd/com.factory.controller.plist.template"
        with template.open("rb") as handle:
            job = plistlib.load(handle)
        self.assertEqual(job["ProcessType"], "Interactive")

    def test_incident_reporter_launch_agent_is_opt_in_and_separate(self) -> None:
        template = ROOT / "scripts/launchd/com.factory.incident-reporter.plist.template"
        text = template.read_text(encoding="utf-8")
        with io.BytesIO(
            text.replace("__HOME__", "/Users/test")
            .replace("__PROJECT_SLUG__", "relay")
            .replace("__ISSUE_REPO__", "nysa-company/software-factory")
            .encode()
        ) as stream:
            job = plistlib.load(stream)
        self.assertEqual(job["StartInterval"], 60)
        self.assertEqual(job["ProgramArguments"], [
            "/Users/test/.factory/bin/factory-launch", "relay",
            "incident-report", "--repo", "nysa-company/software-factory",
            "--json",
        ])

    def test_busy_is_byte_identical_before_controller_construction(self) -> None:
        for directory in ("claims", "passports", ".dispatch-leases"):
            (self.state / directory).mkdir(mode=0o700)
            (self.state / directory / "T-110.json").write_text(
                '{"ticket":"T-110"}\n', encoding="utf-8"
            )
        (self.state / "reconcile.lock").touch(mode=0o600)
        before = {
            path.relative_to(self.state): path.read_bytes()
            for path in self.state.rglob("*") if path.is_file()
        }
        arguments = [
            "factory-controller.py", "--launcher", str(self.launcher),
            "--project", "relay", "--product-root", str(self.product),
            "--release-path", str(self.release), "--state-dir", str(self.state),
        ]
        output = io.StringIO()
        with (
            patch.object(CONTROL.sys, "argv", arguments),
            patch.object(CONTROL.fcntl, "flock", side_effect=BlockingIOError),
            patch.object(CONTROL, "Controller", side_effect=AssertionError),
            redirect_stdout(output),
        ):
            CONTROL.main()
        after = {
            path.relative_to(self.state): path.read_bytes()
            for path in self.state.rglob("*") if path.is_file()
        }
        self.assertEqual(json.loads(output.getvalue())["status"], "busy")
        self.assertEqual(after, before)

    def test_controller_level_external_outage_is_a_safe_wait(self) -> None:
        arguments = [
            "factory-controller.py", "--launcher", str(self.launcher),
            "--project", "relay", "--product-root", str(self.product),
            "--release-path", str(self.release), "--state-dir", str(self.state),
        ]
        controller = Mock()
        controller.reconcile.side_effect = CONTROL.ExternalUnavailable()
        output = io.StringIO()
        with (
            patch.object(CONTROL.sys, "argv", arguments),
            patch.object(CONTROL, "Controller", return_value=controller),
            redirect_stdout(output),
            self.assertRaisesRegex(SystemExit, "75"),
        ):
            CONTROL.main()
        self.assertEqual(json.loads(output.getvalue()), {
            "reason_code": "external_unavailable",
            "status": "wait",
        })

    def test_terminal_event_is_idempotent_across_restart(self) -> None:
        controller = CONTROL.Controller(self.args)
        details = {"protected_main": "b" * 40, "terminal_basis": "attested-done"}
        controller.event_once("operator_terminal_recorded", "T-110", **details)
        controller.event_once("operator_terminal_recorded", "T-110", **details)
        matching = [
            json.loads(path.read_text()) for path in controller.events.glob("*.json")
            if json.loads(path.read_text()).get("event") == "operator_terminal_recorded"
        ]
        self.assertEqual(len(matching), 1)

    def test_external_wait_is_typed_and_does_not_latch_qualification(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.call = lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 75,
            '{"reason_code":"external_unavailable","status":"wait"}\n',
            "",
        )
        with self.assertRaises(CONTROL.ExternalUnavailable):
            controller.json_call("ticket-attest")
        controller.call = lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 1, "", "Could not resolve host: github.com",
        )
        with self.assertRaises(CONTROL.ExternalUnavailable):
            controller.json_call("ci-rerun")

        claim = {
            "branch": "ticket/T-110", "lease": "a" * 64,
            "priority": "normal", "publication_lease": "",
            "receipt": "", "role": "", "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed", "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        controller.ensure_lease = lambda *_args: None
        controller.finish_pending_run = lambda *_args: True
        controller.route_path = lambda *_args: self.product / "route.json"
        (self.product / "route.json").write_text("{}\n")
        controller.refresh_dependency_tracking = lambda *_args: True
        controller.json_call = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(CONTROL.ExternalUnavailable())
        )

        result = controller.reconcile_ticket(claim)

        self.assertEqual(result, {
            "status": "waiting", "ticket": "T-110",
            "wait_reason": "external-unavailable",
        })
        self.assertEqual(claim["blocked_reason"], "external-unavailable")
        self.assertFalse(controller.qualification_cohort_error.is_set())
        with self.assertRaises(CONTROL.ExternalUnavailable):
            CONTROL.require_external_result(
                subprocess.CompletedProcess(
                    ["git", "fetch"], 128, "",
                    "Could not resolve host: github.com",
                ),
                "fetch failed",
            )
        with self.assertRaisesRegex(CONTROL.ControllerError, "fetch failed"):
            CONTROL.require_external_result(
                subprocess.CompletedProcess(
                    ["git", "fetch"], 128, "", "authentication failed",
                ),
                "fetch failed",
            )
        with (
            patch.object(
                CONTROL.subprocess, "run",
                side_effect=subprocess.TimeoutExpired(["git", "fetch"], 120),
            ),
            self.assertRaises(CONTROL.ExternalUnavailable),
        ):
            CONTROL.run_external(["git", "fetch"], "fetch failed")

    def test_exact_push_accepts_lost_response_and_waits_on_outage(self) -> None:
        branch = "ticket/T-110"
        head, before = "b" * 40, "a" * 40
        accepted = [
            subprocess.CompletedProcess([], 128, "", "connection reset by peer"),
            subprocess.CompletedProcess(
                [], 0, f"{head}\trefs/heads/{branch}\n", "",
            ),
            subprocess.CompletedProcess([], 0, before + "\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        with patch.object(CONTROL.subprocess, "run", side_effect=accepted):
            CONTROL.push_exact_head("/cell", branch, head, before)

        unavailable = [
            subprocess.TimeoutExpired(["git", "push"], 120),
            subprocess.CompletedProcess(
                [], 0, f"{before}\trefs/heads/{branch}\n", "",
            ),
        ]
        with (
            patch.object(CONTROL.subprocess, "run", side_effect=unavailable),
            self.assertRaises(CONTROL.ExternalUnavailable),
        ):
            CONTROL.push_exact_head("/cell", branch, head, before)

        controller = CONTROL.Controller(self.args)
        claim = {"branch": branch, "ticket": "T-110", "worktree": "/cell"}
        terminal = {
            "role_branch_before": branch, "role_head_before": before,
            "role_head_after": head, "role_remote_before": before,
            "kit_sha": controller.release_path.name,
        }
        controller.remote_cell_head_status = lambda _claim: (
            "resume_commit_not_pushed", head, before,
        )
        with (
            patch.object(
                CONTROL.subprocess, "run",
                side_effect=[
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, "", ""),
                ],
            ),
            patch.object(CONTROL, "push_exact_head") as resumed,
        ):
            self.assertTrue(controller.resume_push_failed_role(claim, terminal))
        resumed.assert_called_once_with("/cell", branch, head, before)

        terminal["role_head_after"] = "c" * 40
        with patch.object(
            CONTROL.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ):
            self.assertFalse(controller.resume_push_failed_role(claim, terminal))

    def test_concurrent_event_publication_is_monotonic_across_restart(self) -> None:
        controller = CONTROL.Controller(self.args)
        first_started = threading.Event()
        release_first = threading.Event()
        original_replace = CONTROL.os.replace
        replacements = 0

        def delayed_replace(source, destination):
            nonlocal replacements
            if ".controller-event-" in str(source):
                replacements += 1
                if replacements == 1:
                    first_started.set()
                    self.assertTrue(release_first.wait(2))
            return original_replace(source, destination)

        first = threading.Thread(
            target=controller.event, args=("first", "T-110")
        )
        second = threading.Thread(
            target=controller.event, args=("second", "T-111")
        )
        with (
            patch.object(CONTROL.time, "time_ns", return_value=100),
            patch.object(CONTROL.os, "replace", side_effect=delayed_replace),
        ):
            first.start()
            self.assertTrue(first_started.wait(2))
            second.start()
            time.sleep(0.05)
            self.assertEqual(list(controller.events.glob("*.json")), [])
            release_first.set()
            first.join(2)
            second.join(2)
        self.assertFalse(first.is_alive() or second.is_alive())
        records = [
            CONTROL.read(path) for path in sorted(controller.events.glob("*.json"))
        ]
        self.assertEqual([item["event"] for item in records], ["first", "second"])
        self.assertEqual(
            [item["observed_at_epoch_ns"] for item in records], [100, 101]
        )

        restarted = CONTROL.Controller(self.args)
        with patch.object(CONTROL.time, "time_ns", return_value=1):
            restarted.event("third", "T-112")
        records = [
            CONTROL.read(path) for path in sorted(controller.events.glob("*.json"))
        ]
        self.assertEqual(
            [item["event"] for item in records], ["first", "second", "third"]
        )
        self.assertEqual(records[-1]["observed_at_epoch_ns"], 102)

    def operator_transition(
        self, ticket: str, stage: str, role: str | None = None,
        consumed: bool = False, factory_sha: str | None = None,
        head_sha: str | None = None, contract_version: str = "1.8.0",
    ) -> str:
        value = {
            "branch": f"ticket/{ticket}",
            "consumed": consumed,
            "contract_version": contract_version,
            "factory_sha": factory_sha or self.release.name,
            "head_sha": head_sha or "b" * 40,
            "project": "relay",
            "role": role,
            "schema": "nysa.software-factory.transition-receipt/v1",
            "stage": stage,
            "ticket": ticket,
        }
        value["receipt_sha256"] = hashlib.sha256(STATE.canonical({
            key: item for key, item in value.items()
            if key not in {"consumed", "receipt_sha256"}
        })).hexdigest()
        CONTROL.write(self.state / f"{ticket}.json", value)
        return value["receipt_sha256"]

    def operator_passport(
        self, ticket: str, current_state: str, publication_state: str,
        transition_receipt_sha256: str = "", head_sha: str | None = None,
        branch: str | None = None, factory_sha: str | None = None,
        contract_version: str = "1.8.0",
    ) -> str:
        key_path = self.state / "passport.key"
        if not key_path.exists():
            key_path.write_bytes(b"k" * 32)
            key_path.chmod(0o600)
        body = {
            "branch": branch or f"ticket/{ticket}",
            "contract_version": contract_version,
            "current_state": current_state,
            "factory_sha": factory_sha or self.release.name,
            "project": "relay",
            "publication_state": publication_state,
            "schema": "nysa.software-factory.ticket-passport/v1",
            "ticket": ticket,
            "transition_receipt_sha256": transition_receipt_sha256,
        }
        if head_sha is not None:
            body["head_sha"] = head_sha
        body = PASSPORT.authenticate(body, key_path.read_bytes())
        passports = self.state / "passports"
        passports.mkdir(mode=0o700, exist_ok=True)
        PASSPORT.write_atomic(passports / f"{ticket}.json", body)
        return body["passport_sha256"]

    def test_operator_passport_accepts_writer_and_state_machine_wire_bytes(
        self,
    ) -> None:
        ticket = "T-110"
        digest = self.operator_passport(ticket, "Building", "none")
        path = self.state / f"passports/{ticket}.json"
        raw = path.read_bytes()

        self.assertEqual(raw, PASSPORT.canonical(json.loads(raw)))
        self.assertEqual(
            CONTROL.Controller(self.args).authenticated_operator_passport(ticket)[
                "passport_sha256"
            ],
            digest,
        )
        state_passport, _secret = STATE.authenticated_passport(
            argparse.Namespace(state_dir=self.state, ticket=ticket)
        )
        self.assertEqual(state_passport["passport_sha256"], digest)

    def test_controller_accepts_only_current_contract_receipts_and_passports(
        self,
    ) -> None:
        ticket = "T-110"
        claim = {"branch": f"ticket/{ticket}", "ticket": ticket}
        for contract in ("1.8.0", "2.0.0"):
            with self.subTest(contract=contract):
                receipt = self.operator_transition(
                    ticket, "RUN planner", role="planner",
                    contract_version=contract,
                )
                passport = self.operator_passport(
                    ticket, "Planning", "none", receipt,
                    contract_version=contract,
                )
                controller = CONTROL.Controller(self.args)
                self.assertEqual(
                    controller.transition_receipt(claim)["contract_version"],
                    contract,
                )
                self.assertEqual(
                    controller.authenticated_operator_passport(ticket)[
                        "contract_version"
                    ],
                    contract,
                )

        for contract in ("1.7.0", "1.9.0"):
            with self.subTest(contract=contract):
                self.operator_transition(
                    ticket, "RUN planner", role="planner",
                    contract_version=contract,
                )
                self.operator_passport(
                    ticket, "Planning", "none", contract_version=contract,
                )
                controller = CONTROL.Controller(self.args)
                self.assertIsNone(controller.transition_receipt(claim))
                with self.assertRaisesRegex(
                    CONTROL.ControllerError, "passport identity is invalid",
                ):
                    controller.authenticated_operator_passport(ticket)

    def test_operator_events_backfill_each_durable_crash_boundary_once(self) -> None:
        controller = CONTROL.Controller(self.args)
        claims = []

        budget_ticket = "T-110"
        self.operator_transition(budget_ticket, "AWAIT_BUDGET daily envelope")
        claims.append({
            "branch": f"ticket/{budget_ticket}",
            "budget_sha256": controller.envelope_digest(), "lease": "1" * 64,
            "receipt": "", "role": "", "status": "budget",
            "ticket": budget_ticket,
        })

        approval_ticket = "T-111"
        self.operator_transition(
            approval_ticket, "AWAIT-OPERATOR bundle posted; approval required",
            consumed=True,
        )
        approval_passport = self.operator_passport(
            approval_ticket, "Awaiting Approval", "validating",
        )
        claims.append({
            "branch": f"ticket/{approval_ticket}", "lease": "2" * 64,
            "receipt": "", "role": "", "status": "waiting",
            "ticket": approval_ticket,
        })

        role_ticket = "T-112"
        role_receipt = self.operator_transition(
            role_ticket, "RUN builder", "builder", consumed=True,
        )
        role_passport = self.operator_passport(
            role_ticket, "Building", "none", role_receipt,
        )
        (self.product / "factory/runs/role-failure.meta").write_text(
            "run_id=role-failure\n"
            "phase=completed\n"
            f"ticket={role_ticket}\n"
            "role=builder\n"
            "accounting_state=completed\n"
            "go_issued=1\n"
            "task_submitted=1\n"
            "effective_cost=1\n"
            "cost_basis=provider_reported\n"
            "exit_status=1\n"
            "role_exit=provider_failed\n"
            f"kit_sha={self.release.name}\n"
            f"role_branch_before=ticket/{role_ticket}\n"
            f"role_head_before={'b' * 40}\n"
            "terminal_reason_code=soft_timeout\n"
            f"transition_receipt_sha256={role_receipt}\n",
            encoding="utf-8",
        )
        claims.append({
            "blocked_reason": "role-failure",
            "branch": f"ticket/{role_ticket}", "lease": "3" * 64,
            "receipt": role_receipt, "role": "builder", "status": "blocked",
            "ticket": role_ticket,
        })

        escalation_ticket = "T-113"
        self.operator_transition(
            escalation_ticket, "ESCALATE evidence bundle invalid",
        )
        claims.append({
            "blocked_reason": "state-machine-escalation",
            "branch": f"ticket/{escalation_ticket}", "lease": "4" * 64,
            "receipt": "", "role": "", "status": "blocked",
            "ticket": escalation_ticket,
        })

        pre_go_ticket = "T-114"
        pre_go_receipt = self.operator_transition(
            pre_go_ticket, "RUN narrator", "narrator",
        )
        (self.product / "factory/runs/pre-go.meta").write_text(
            "run_id=pre-go\n"
            "phase=abandoned\n"
            f"ticket={pre_go_ticket}\n"
            "role=narrator\n"
            "accounting_state=launch_void\n"
            "go_issued=0\n"
            "task_submitted=0\n"
            "effective_cost=0\n"
            "cost_basis=launch_void\n"
            "exit_status=6\n"
            "role_exit=\n"
            f"kit_sha={self.release.name}\n"
            f"role_branch_before=ticket/{pre_go_ticket}\n"
            f"role_head_before={'b' * 40}\n"
            "terminal_reason_code=cursor_credential_unsafe\n"
            f"transition_receipt_sha256={pre_go_receipt}\n",
            encoding="utf-8",
        )
        claims.append({
            "blocked_reason": "pre-go-failure",
            "branch": f"ticket/{pre_go_ticket}", "lease": "5" * 64,
            "receipt": pre_go_receipt, "role": "narrator", "status": "blocked",
            "ticket": pre_go_ticket,
        })

        fallback_ticket = "T-115"
        claims.append({
            "blocked_reason": "qualification-fallback-refused:manifest:"
            + controller.release_path.name,
            "branch": f"ticket/{fallback_ticket}", "lease": "6" * 64,
            "receipt": "", "role": "", "status": "blocked",
            "ticket": fallback_ticket,
        })

        class InjectedCrash(BaseException):
            pass

        for name, ticket in (
            ("budget_wait", budget_ticket),
            ("awaiting_approval", approval_ticket),
            ("role_blocked", role_ticket),
            ("ticket_blocked", escalation_ticket),
            ("pre_go_failure_blocked", pre_go_ticket),
            ("typed_recovery_refused", fallback_ticket),
        ):
            with (
                patch.object(controller, "event", side_effect=InjectedCrash),
                self.assertRaises(InjectedCrash),
            ):
                controller.event(name, ticket)

        controller.recover_operator_action_events(claims)
        CONTROL.Controller(self.args).recover_operator_action_events(claims)
        events = [
            CONTROL.read(path) for path in self.state.glob("events/*.json")
        ]
        expected = {
            "budget_wait", "awaiting_approval", "role_blocked",
            "ticket_blocked", "state_machine_escalated",
            "pre_go_failure_blocked",
            "typed_recovery_refused",
        }
        self.assertEqual(
            {event["event"] for event in events}, expected,
        )
        for name in expected:
            self.assertEqual(
                len([event for event in events if event["event"] == name]), 1,
            )
        self.assertEqual(
            next(event for event in events if event["event"] == "budget_wait")
            ["passport_sha256"],
            None,
        )
        self.assertEqual(
            next(event for event in events if event["event"] == "awaiting_approval")
            ["passport_sha256"],
            approval_passport,
        )
        self.assertEqual(
            next(event for event in events if event["event"] == "role_blocked")
            ["passport_sha256"],
            role_passport,
        )

    def test_contract_block_migration_event_backfills_after_crash(self) -> None:
        controller = CONTROL.Controller(self.args)
        ticket = "T-110"
        head = "c" * 40
        receipt = self.operator_transition(
            ticket, "RUN planner", "planner", consumed=True,
        )
        self.operator_passport(
            ticket, "Blocked-Escalated", "none", receipt,
            head_sha=head,
        )
        claim = {
            "blocked_reason": "role-failure",
            "branch": f"ticket/{ticket}", "lease": "a" * 64,
            "receipt": receipt, "role": "planner", "status": "blocked",
            "ticket": ticket, "worktree": str(self.product),
        }
        controller.terminal_for_receipt = lambda *_args: {
            "exit_status": "12", "role": "planner",
            "role_exit": "role_exit_contract_blocked",
            "run_id": "contract-block", "ticket": ticket,
            "transition_receipt_sha256": receipt,
        }
        controller.json_call = lambda *_args, **_kwargs: {
            "action": "repair-check", "head": head, "role": "planner",
            "schema": "nysa.software-factory.state-machine/v1",
            "status": "waiting", "ticket": ticket,
        }
        ticket_path = self.product / "factory/tickets/T-110.md"
        ticket_path.parent.mkdir(parents=True, exist_ok=True)
        ticket_path.write_text(
            "# T-110\n\nState: Blocked-Escalated\n"
            "OPERATOR ANSWER: Preserve this context.\n"
            f"OPERATOR ANSWER RECEIPT: {receipt}\n",
            encoding="utf-8",
        )
        controller.restore_recorded_contract_repair = lambda _claim: False
        controller.restore_contract_blocker = lambda _claim: None
        controller.role_active = lambda _claim: False
        controller.direct_model_identity_candidate = lambda *_args: False
        controller.remote_cell_head_status = lambda _claim: (
            "pushed", head, head,
        )
        migrated = False
        controller.remote_passport_valid = lambda _claim: migrated

        def migrate(_claim, publication, expected_head):
            nonlocal migrated
            self.assertEqual((publication, expected_head), ("preserve", head))
            migrated = True

        controller.migrate_passport = migrate
        class InjectedCrash(BaseException):
            pass

        with (
            patch.object(controller, "event", side_effect=InjectedCrash),
            self.assertRaises(InjectedCrash),
        ):
            controller.recover_repaired_failures([claim])

        self.assertTrue(migrated)
        controller.remote_passport_valid = lambda _claim: False
        controller.recover_operator_action_events([claim])
        self.assertEqual(list(controller.events.glob("*.json")), [])
        controller.remote_passport_valid = lambda _claim: True
        controller.recover_operator_action_events([claim])
        restarted = CONTROL.Controller(self.args)
        restarted.terminal_for_receipt = controller.terminal_for_receipt
        restarted.json_call = controller.json_call
        restarted.remote_passport_valid = controller.remote_passport_valid
        restarted.recover_operator_action_events([claim])
        events = [
            CONTROL.read(path) for path in controller.events.glob("*.json")
            if CONTROL.read(path).get("event")
            == "contract_block_passport_migrated"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["failed_run_id"], "contract-block")

    def test_multi_kit_receipts_leave_terminal_inert_and_sibling_runnable(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        controller.capacity = 1
        claims = []
        for ticket, release in (("T-110", "b" * 40), ("T-111", "c" * 40)):
            cell = self.root / ticket
            cell.mkdir()
            self.operator_transition(
                ticket, "RUN planner", role="planner", factory_sha=release,
            )
            claim = {
                "blocked_reason": "preflight",
                "branch": f"ticket/{ticket}",
                "lease": ticket[-1] * 64,
                "lease_released": True,
                "priority": "normal", "publication_lease": "",
                "receipt": "", "role": "", "schema": CONTROL.CLAIM_SCHEMA,
                "status": "blocked", "ticket": ticket, "worktree": str(cell),
            }
            controller.save_claim(claim)
            claims.append(claim)

        terminal = "T-109"
        terminal_cell = self.root / terminal
        terminal_cell.mkdir()
        self.operator_transition(
            terminal, "COMPLETE protected terminal", factory_sha="d" * 40,
        )
        terminal_claim = {
            "branch": f"ticket/{terminal}", "lease": "9" * 64,
            "priority": "normal", "publication_lease": "", "receipt": "",
            "role": "", "schema": CONTROL.CLAIM_SCHEMA, "status": "waiting",
            "ticket": terminal, "worktree": str(terminal_cell),
        }
        controller.save_claim(terminal_claim)
        claims.append(terminal_claim)

        sibling = "T-112"
        sibling_cell = self.root / sibling
        route = sibling_cell / f"factory/route-plans/{sibling}.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        sibling_digest = self.operator_transition(
            sibling, "AWAIT-OPERATOR bundle posted; approval required",
            consumed=True,
        )
        self.operator_passport(sibling, "Awaiting Approval", "validating")
        sibling_claim = {
            "branch": f"ticket/{sibling}", "lease": "2" * 64,
            "priority": "normal", "publication_lease": "", "receipt": "",
            "role": "", "schema": CONTROL.CLAIM_SCHEMA, "status": "waiting",
            "ticket": sibling, "worktree": str(sibling_cell),
        }
        controller.save_claim(sibling_claim)
        claims.append(sibling_claim)
        self.assertEqual(
            controller.operator_transition(sibling_claim)["receipt_sha256"],
            sibling_digest,
        )

        tampered = "T-113"
        tampered_cell = self.root / tampered
        tampered_cell.mkdir()
        self.operator_transition(tampered, "ESCALATE operator decision required")
        tampered_path = self.state / f"{tampered}.json"
        tampered_receipt = CONTROL.read(tampered_path)
        tampered_receipt["stage"] = "ESCALATE altered evidence"
        CONTROL.write(tampered_path, tampered_receipt)
        tampered_claim = {
            "branch": f"ticket/{tampered}", "lease": "3" * 64,
            "priority": "normal", "publication_lease": "", "receipt": "",
            "role": "", "schema": CONTROL.CLAIM_SCHEMA, "status": "claimed",
            "ticket": tampered, "worktree": str(tampered_cell),
        }
        controller.save_claim(tampered_claim)
        claims.append(tampered_claim)

        tampered_preflight = "T-114"
        preflight_cell = self.root / tampered_preflight
        preflight_cell.mkdir()
        self.operator_transition(
            tampered_preflight, "AWAIT_BUDGET daily envelope", role="planner",
        )
        preflight_path = self.state / f"{tampered_preflight}.json"
        preflight_receipt = CONTROL.read(preflight_path)
        preflight_receipt["stage"] = "RUN planner"
        CONTROL.write(preflight_path, preflight_receipt)
        preflight_claim = {
            "blocked_reason": "preflight",
            "branch": f"ticket/{tampered_preflight}", "lease": "4" * 64,
            "lease_released": True, "priority": "normal",
            "publication_lease": "", "receipt": "", "role": "",
            "schema": CONTROL.CLAIM_SCHEMA, "status": "blocked",
            "ticket": tampered_preflight, "worktree": str(preflight_cell),
        }
        controller.save_claim(preflight_claim)
        claims.append(preflight_claim)

        release_refused = "T-115"
        refused_cell = self.root / release_refused
        refused_cell.mkdir()
        self.operator_transition(release_refused, "RUN planner", role="planner")
        refused_path = self.state / f"{release_refused}.json"
        refused_receipt = CONTROL.read(refused_path)
        refused_receipt["stage"] = "RUN builder"
        CONTROL.write(refused_path, refused_receipt)
        refused_claim = {
            "branch": f"ticket/{release_refused}", "lease": "5" * 64,
            "priority": "normal", "publication_lease": "", "receipt": "",
            "role": "", "schema": CONTROL.CLAIM_SCHEMA, "status": "claimed",
            "ticket": release_refused, "worktree": str(refused_cell),
        }
        controller.save_claim(refused_claim)
        claims.append(refused_claim)

        reconciled = []
        leased = []
        preflight_candidates = []
        upgrade_candidates = []
        controller_calls = []
        controller.cancellation_authority = lambda _claims: None
        controller.product_ticket_done = lambda ticket: ticket == terminal
        controller.ensure_lease = lambda claim, _label: leased.append(
            claim["ticket"]
        )
        controller.release = lambda claim: controller.claim_path(
            claim["ticket"]
        ).unlink()
        controller.record_qualification_done_targets = lambda: None
        controller.recover_missing_passport_claims = lambda _claims: None
        controller.recover_terminal_requests = lambda _claims: None
        for name in (
            "recover_interrupted_claims", "recover_missing_terminals",
            "recover_passportless_route_migrations",
            "recover_prepublication_attestations", "recover_terminal_exports",
            "recover_repaired_failures",
        ):
            setattr(controller, name, lambda _claims: None)
        controller.recover_preflight_blocks = lambda group: (
            preflight_candidates.extend(claim["ticket"] for claim in group)
        )
        controller.recover_upgraded_claims = lambda group: (
            upgrade_candidates.extend(claim["ticket"] for claim in group)
        )
        def json_call(*args, **_kwargs):
            controller_calls.append(args)
            if args[:3] == ("release", "--ticket", release_refused):
                raise CONTROL.ControllerError("fixture release refused")
            return {"action": "WAIT"}

        controller.json_call = json_call
        controller.clear_admission_failure = lambda: None
        controller.pin_routes = lambda _claims: []
        controller.reconcile_ticket_until_wait = lambda claim: (
            reconciled.append(claim["ticket"])
            or {"status": "waiting", "ticket": claim["ticket"]}
        )

        result = controller.reconcile()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(reconciled, [sibling])
        self.assertNotIn(tampered, leased)
        self.assertEqual(
            [call for call in controller_calls if call[0] == "release"],
            [
                ("release", "--ticket", tampered, "--lease", "3" * 64),
                (
                    "release", "--ticket", release_refused,
                    "--lease", "5" * 64,
                ),
                ("release", "--ticket", sibling, "--lease", "2" * 64),
            ],
        )
        self.assertTrue(
            CONTROL.read(controller.claim_path(tampered))["lease_released"]
        )
        self.assertNotIn(
            "lease_released", CONTROL.read(controller.claim_path(release_refused))
        )
        self.assertTrue(
            CONTROL.read(controller.claim_path(sibling))["lease_released"]
        )
        self.assertTrue({"T-110", "T-111"}.issubset(upgrade_candidates))
        self.assertFalse(
            {
                "T-110", "T-111", tampered, tampered_preflight,
                release_refused,
            }.intersection(preflight_candidates)
        )
        dispatches = [
            call for call in controller_calls if call[0] == "dispatch-plan"
        ]
        self.assertTrue(dispatches)
        for arguments in dispatches:
            excluded = {
                arguments[index + 1]
                for index, item in enumerate(arguments)
                if item == "--exclude-ticket"
            }
            self.assertTrue({
                "T-110", "T-111", tampered, tampered_preflight,
                release_refused,
            }.issubset(excluded))
        self.assertFalse(controller.claim_path(terminal).exists())
        events = [
            CONTROL.read(path) for path in self.state.glob("events/*.json")
        ]
        stale_events = [
            event for event in events
            if event["event"] == "prior_kit_transition_receipt_observed"
        ]
        self.assertEqual(
            sorted(event["ticket"] for event in stale_events),
            ["T-110", "T-111"],
        )
        self.assertNotIn(terminal, [event["ticket"] for event in stale_events])
        self.assertIn(
            (tampered, "receipt_digest_invalid"),
            [
                (event["ticket"], event.get("reason_code")) for event in events
                if event["event"] == "transition_receipt_invalid"
            ],
        )
        self.assertNotIn(
            tampered,
            [
                event["ticket"] for event in events
                if event["event"] == "state_machine_escalated"
            ],
        )

        restarted = CONTROL.Controller(self.args)
        restarted.recover_operator_action_events(restarted.load_claims())
        self.assertEqual(
            len([
                path for path in self.state.glob("events/*.json")
                if CONTROL.read(path).get("event")
                == "prior_kit_transition_receipt_observed"
            ]),
            2,
        )
        second = controller.reconcile()
        self.assertEqual(second["status"], "ok")
        self.assertEqual(reconciled, [sibling, sibling])
        self.assertEqual(
            len([
                path for path in self.state.glob("events/*.json")
                if CONTROL.read(path).get("event")
                == "transition_receipt_quarantine_waiting"
            ]),
            1,
        )
        self.assertEqual(
            len([
                path for path in self.state.glob("events/*.json")
                if CONTROL.read(path).get("event")
                == "transition_receipt_invalid"
            ]),
            3,
        )

    def test_invalid_transition_quarantine_preserves_active_role(self) -> None:
        controller = CONTROL.Controller(self.args)
        active = {
            "branch": "ticket/T-110", "lease": "1" * 64,
            "receipt": "", "role": "", "status": "claimed",
            "ticket": "T-110",
        }
        waiting = {
            "branch": "ticket/T-111", "lease": "2" * 64,
            "receipt": "", "role": "", "status": "waiting",
            "ticket": "T-111",
        }
        for claim in (active, waiting):
            self.operator_transition(claim["ticket"], "RUN planner", role="planner")
            path = self.state / f"{claim['ticket']}.json"
            receipt = CONTROL.read(path)
            receipt["stage"] = "RUN builder"
            CONTROL.write(path, receipt)
            self.assertIsNone(controller.operator_transition(claim))
        released = []
        controller.role_active = lambda claim: claim["ticket"] == "T-110"
        controller.release_ticket_lease = lambda claim: (
            released.append(claim["ticket"]),
            claim.update(lease_released=True),
        )

        controller.quarantine_invalid_transition_claims([active, waiting])

        self.assertEqual(released, ["T-111"])
        self.assertTrue(controller.consumes_capacity(active))
        self.assertTrue(waiting["lease_released"])

    def test_operator_event_backfill_refuses_tampered_passport(self) -> None:
        ticket = "T-110"
        self.operator_transition(
            ticket, "AWAIT-OPERATOR bundle posted; approval required",
            consumed=True,
        )
        self.operator_passport(ticket, "Awaiting Approval", "validating")
        passport = self.state / f"passports/{ticket}.json"
        value = CONTROL.read(passport)
        value["current_state"] = "Done"
        CONTROL.write(passport, value)
        claim = {
            "branch": f"ticket/{ticket}", "lease": "1" * 64,
            "receipt": "", "role": "", "status": "waiting", "ticket": ticket,
        }
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "passport digest is invalid",
        ):
            CONTROL.Controller(self.args).recover_operator_action_events([claim])
        self.assertEqual(list((self.state / "events").glob("*.json")), [])

    def test_operator_event_backfill_refuses_wrong_passport_hmac(self) -> None:
        ticket = "T-110"
        self.operator_transition(
            ticket, "AWAIT-OPERATOR bundle posted; approval required",
            consumed=True,
        )
        self.operator_passport(ticket, "Awaiting Approval", "validating")
        passport = self.state / f"passports/{ticket}.json"
        value = CONTROL.read(passport)
        value.pop("passport_sha256")
        value["authentication_sha256"] = "0" * 64
        value["passport_sha256"] = hashlib.sha256(
            PASSPORT.canonical(value)
        ).hexdigest()
        PASSPORT.write_atomic(passport, value)
        claim = {
            "branch": f"ticket/{ticket}", "lease": "1" * 64,
            "receipt": "", "role": "", "status": "waiting", "ticket": ticket,
        }
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "passport authentication is invalid",
        ):
            CONTROL.Controller(self.args).recover_operator_action_events([claim])
        self.assertEqual(list((self.state / "events").glob("*.json")), [])

    def test_operator_event_backfill_binds_passport_project_and_contract(
        self,
    ) -> None:
        ticket = "T-110"
        self.operator_transition(
            ticket, "AWAIT-OPERATOR bundle posted; approval required",
            consumed=True,
        )
        claim = {
            "branch": f"ticket/{ticket}", "lease": "1" * 64,
            "receipt": "", "role": "", "status": "waiting", "ticket": ticket,
        }
        key = (self.state / "passport.key")
        passport_path = self.state / f"passports/{ticket}.json"
        for field, wrong in (("project", "another"), ("contract_version", "1.7.0")):
            with self.subTest(field=field):
                self.operator_passport(ticket, "Awaiting Approval", "validating")
                value = CONTROL.read(passport_path)
                value.pop("passport_sha256")
                value.pop("authentication_sha256")
                value[field] = wrong
                PASSPORT.write_atomic(
                    passport_path, PASSPORT.authenticate(value, key.read_bytes())
                )
                with self.assertRaisesRegex(
                    CONTROL.ControllerError, "passport identity is invalid",
                ):
                    CONTROL.Controller(self.args).recover_operator_action_events(
                        [claim]
                    )
        self.assertEqual(list((self.state / "events").glob("*.json")), [])

    def test_operator_event_backfill_inventories_history_once(self) -> None:
        controller = CONTROL.Controller(self.args)
        for number in range(12):
            controller.event("unrelated", f"T-{200 + number}")
        claim = {
            "blocked_reason": "preflight", "branch": "ticket/T-110",
            "lease": "1" * 64, "receipt": "", "role": "",
            "status": "blocked", "ticket": "T-110",
        }
        original_read = CONTROL.read
        reads = 0

        def counted(path):
            nonlocal reads
            if path.parent == controller.events:
                reads += 1
            return original_read(path)

        with patch.object(CONTROL, "read", side_effect=counted):
            controller.recover_operator_action_events([claim] * 3)
        self.assertEqual(reads, 12)
        events = [
            original_read(path) for path in controller.events.glob("*.json")
        ]
        self.assertEqual(
            len([event for event in events if event["event"] == "ticket_blocked"]),
            1,
        )

    def test_contract_resume_refusal_is_restart_safe_and_ticket_scoped(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = {"receipt": "c" * 64, "ticket": "T-110"}
        evidence = {
            "local_head": "b" * 40,
            "remote_head": "a" * 40,
        }
        controller.record_contract_resume_refusal(
            claim, "resume_commit_not_pushed", evidence
        )
        CONTROL.Controller(self.args).record_contract_resume_refusal(
            claim, "resume_commit_not_pushed", evidence
        )
        claim["receipt"] = "d" * 64
        controller.record_contract_resume_refusal(
            claim, "resume_commit_not_pushed", evidence
        )
        controller.record_contract_resume_refusal(
            {"receipt": "e" * 64, "ticket": "T-111"},
            "resume_commit_not_pushed", evidence,
        )
        controller.record_contract_resume_refusal(
            {"receipt": "f" * 64, "ticket": "T-112"},
            "resume_parent_not_migrated", {"offending_parent": "9" * 40},
        )
        refusals = [
            CONTROL.read(path) for path in controller.events.glob("*.json")
            if CONTROL.read(path).get("event") == "contract_resume_refused"
        ]
        self.assertEqual(
            sorted(item["ticket"] for item in refusals),
            ["T-110", "T-110", "T-111", "T-112"],
        )

    def test_operator_answer_without_resume_stays_waiting_during_sweep(self) -> None:
        receipt = "c" * 64
        ticket = (
            "# T-110\n\nState: Blocked-Escalated\n"
            "OPERATOR ANSWER: Preserve the isolated fixture seam.\n"
            f"OPERATOR ANSWER RECEIPT: {receipt}\n"
        )
        self.assertEqual(
            CONTROL.Controller.contract_resume_directive_status(ticket, receipt),
            "waiting",
        )
        later = (
            ticket
            + "OPERATOR RESUME: builder\n"
            + f"OPERATOR RESUME RECEIPT: {'b' * 64}\n"
        )
        self.assertEqual(
            CONTROL.Controller.contract_resume_directive_status(later, receipt),
            "waiting",
        )

    def test_remote_cell_head_status_distinguishes_unpushed_from_diverged(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = {
            "branch": "ticket/T-110",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        local = "b" * 40
        remote = "a" * 40
        responses = [
            CONTROL.subprocess.CompletedProcess([], 0, local + "\n", ""),
            CONTROL.subprocess.CompletedProcess(
                [], 0, f"{remote}\trefs/heads/{claim['branch']}\n", ""
            ),
            CONTROL.subprocess.CompletedProcess([], 0, "", ""),
        ]
        with patch.object(CONTROL.subprocess, "run", side_effect=responses):
            self.assertEqual(
                controller.remote_cell_head_status(claim),
                ("resume_commit_not_pushed", local, remote),
            )
        responses[-1] = CONTROL.subprocess.CompletedProcess([], 1, "", "")
        with patch.object(CONTROL.subprocess, "run", side_effect=responses):
            self.assertEqual(
                controller.remote_cell_head_status(claim),
                ("resume_ancestry_invalid", local, remote),
            )
        responses[-1] = CONTROL.subprocess.CompletedProcess([], 128, "", "")
        with patch.object(CONTROL.subprocess, "run", side_effect=responses):
            self.assertEqual(
                controller.remote_cell_head_status(claim),
                ("remote_unavailable", local, remote),
            )

    def test_qualification_events_bind_the_exact_manifest_generation(self) -> None:
        manifest = {
            "budget_usd": "100.000000",
            "capacity": 4,
            "contract_version": "1.8.0",
            "factory_sha": "a" * 40,
            "generation": 27,
            "per_run_budget_usd": "2.000000",
            "per_ticket_budget_usd": "25.000000",
            "schema": CONTROL.QUALIFICATION_SCHEMA,
            "target_done": 4,
            "tickets": ["T-110", "T-111", "T-112", "T-113"],
        }
        (self.product / "factory/QUALIFICATION.json").write_text(
            json.dumps(manifest), encoding="utf-8",
        )
        controller = CONTROL.Controller(self.args)
        controller.event("restart_boundary", tickets=manifest["tickets"])
        event = CONTROL.read(next(controller.events.glob("*.json")))
        self.assertEqual(event["qualification_generation"], 27)
        self.assertEqual(
            event["qualification_manifest_sha256"],
            hashlib.sha256(CONTROL.canonical(manifest).encode()).hexdigest(),
        )

    def test_qualification_reconciles_protected_terminal_without_passport(self) -> None:
        tickets = [f"T-{number}" for number in range(110, 114)]
        (self.product / "factory/QUALIFICATION.json").write_text(json.dumps({
            "budget_usd": "100.000000",
            "capacity": 4,
            "contract_version": "1.8.0",
            "factory_sha": "a" * 40,
            "generation": 1,
            "per_run_budget_usd": "2.000000",
            "per_ticket_budget_usd": "25.000000",
            "schema": CONTROL.QUALIFICATION_SCHEMA,
            "target_done": 4,
            "tickets": tickets,
        }), encoding="utf-8")
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.parent.mkdir()
        ticket.write_text("State: Done\n", encoding="utf-8")
        done = self.product / "factory/attestations/T-110/done.json"
        done.parent.mkdir(parents=True)
        done.write_text('{"ticket":"T-110"}\n', encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.product)], check=True)
        subprocess.run(
            ["git", "-C", str(self.product), "config", "user.email", "test@nysa.dev"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.product), "config", "user.name", "Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.product), "add", "factory"], check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.product), "commit", "-qm", "terminal"],
            check=True,
        )
        subprocess.run(
            [
                "git", "-C", str(self.product), "update-ref",
                "refs/remotes/origin/main", "HEAD",
            ],
            check=True,
        )
        ticket.write_text("State: Ready\n", encoding="utf-8")
        controller = CONTROL.Controller(self.args)
        with patch.object(CONTROL, "protected_terminal", return_value={
            "basis": "attested-emergency-closeout", "ticket": "T-110",
        }):
            self.assertTrue(controller.product_ticket_done("T-110"))
            controller.record_qualification_done_targets()
            controller.record_qualification_done_targets()
        records = [CONTROL.read(path) for path in controller.events.glob("*.json")]
        self.assertEqual(sum(
            item.get("event") == "protected_terminal_reconciled"
            for item in records
        ), 1)
        self.assertEqual(sum(
            item.get("event") == "ticket_complete" for item in records
        ), 1)
        self.assertFalse((self.state / "passports").exists())
        self.assertEqual(list(controller.claims.iterdir()), [])

    def test_qualification_reconciles_exact_emergency_terminal_with_passport(self) -> None:
        ticket = "T-110"
        source = "b" * 40
        terminal_factory = "e" * 40
        terminal_receipt = "9" * 64
        head = "c" * 40
        passport_sha = "d" * 64
        (self.product / "factory/PROJECT.env").write_text(
            "MAX_CONCURRENT_TICKETS=3\n", encoding="utf-8",
        )
        (self.product / "factory/QUALIFICATION.json").write_text(json.dumps({
            "budget_usd": "300.000000",
            "capacity": 3,
            "contract_version": "1.8.0",
            "factory_sha": "a" * 40,
            "generation": 2,
            "mode": "successor",
            "per_run_budget_usd": "10.000000",
            "per_ticket_budget_usd": "100.000000",
            "schema": CONTROL.QUALIFICATION_SCHEMA,
            "source_factory_sha": source,
            "target_done": 3,
            "tickets": ["T-110", "T-111", "T-112"],
        }), encoding="utf-8")
        ticket_path = self.product / f"factory/tickets/{ticket}.md"
        ticket_path.parent.mkdir()
        ticket_path.write_text("State: Done\n", encoding="utf-8")
        passport_path = self.state / f"passports/{ticket}.json"
        passport_path.parent.mkdir(mode=0o700)
        passport = {
            "branch": f"ticket/{ticket}",
            "current_state": "Review",
            "factory_sha": source,
            "head_sha": head,
            "passport_sha256": passport_sha,
            "publication_state": "validating",
            "ticket": ticket,
        }
        CONTROL.write(passport_path, passport)
        pause = {
            "branch": f"ticket/{ticket}",
            "budget_sha256": "1" * 64,
            "current_state": "Review",
            "head_sha": head,
            "passport_factory_sha": source,
            "passport_sha256": passport_sha,
            "schema": "nysa.software-factory.ticket-pause/v2",
            "status": "budget",
            "ticket": ticket,
        }
        pause["pause_sha256"] = hashlib.sha256(
            CONTROL.canonical(pause).encode()
        ).hexdigest()
        pause_path = self.state / f"pause-{ticket}.json"
        CONTROL.write(pause_path, pause)
        pause_file = hashlib.sha256(
            (CONTROL.canonical(pause) + "\n").encode()
        ).hexdigest()
        done_path = self.product / f"factory/attestations/{ticket}/done.json"
        done_path.parent.mkdir(parents=True)
        done = {
            "kit_sha": terminal_factory,
            "plan": {
                "claim": {
                    "blocked_reason": "factory-issue-pause",
                    "parked": True,
                    "receipt": pause["pause_sha256"],
                    "role": "factory-paused",
                    "sha256": pause_file,
                    "status": "blocked",
                },
                "execution_basis": "authenticated-passport",
                "kit_sha": terminal_factory,
                "passport": {
                    name: passport[name]
                    for name in (
                        "passport_sha256", "current_state", "publication_state",
                        "factory_sha", "head_sha",
                    )
                },
            },
            "schema": "nysa.software-factory.ticket-emergency-done/v2",
        }
        done_path.write_text(json.dumps(done), encoding="utf-8")
        protected = {
            "done_sha256": hashlib.sha256(
                CONTROL.canonical(done).encode()
            ).hexdigest(),
            "protected_main_sha": "2" * 40,
            "protected_main_tree": "3" * 40,
            "protected_ticket_blob": "4" * 40,
            "qualification_charge_micro_usd": 0,
            "reconciliation_schema": (
                CONTROL.PROTECTED_TERMINAL_RECONCILIATION_SCHEMA
            ),
            "terminal_basis": "attested-emergency-closeout",
        }
        controller = CONTROL.Controller(self.args)
        controller.qualification_protected_terminal = lambda _ticket: protected
        controller.qualification_release_receipts = lambda: {
            "a" * 40: "8" * 64,
            terminal_factory: terminal_receipt,
        }

        result = controller.qualification_emergency_terminal(ticket)
        self.assertEqual(
            result["reconciliation_schema"],
            CONTROL.EMERGENCY_TERMINAL_RECONCILIATION_SCHEMA,
        )
        self.assertEqual(result["source_passport_sha256"], passport_sha)
        self.assertEqual(result["terminal_factory_sha"], terminal_factory)
        self.assertEqual(result["terminal_release_receipt_id"], terminal_receipt)
        controller.qualification_release_receipts = lambda: {"a" * 40: "8" * 64}
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "emergency terminal evidence is invalid",
        ):
            controller.qualification_emergency_terminal(ticket)
        controller.qualification_release_receipts = lambda: {
            "a" * 40: "8" * 64,
            terminal_factory: terminal_receipt,
        }
        changed_done = copy.deepcopy(done)
        changed_done["plan"]["kit_sha"] = "f" * 40
        done_path.write_text(json.dumps(changed_done), encoding="utf-8")
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "emergency terminal evidence is invalid",
        ):
            controller.qualification_emergency_terminal(ticket)
        done_path.write_text(json.dumps(done), encoding="utf-8")
        changed = copy.deepcopy(pause)
        changed["budget_sha256"] = "short"
        changed["pause_sha256"] = hashlib.sha256(
            CONTROL.canonical({
                name: value for name, value in changed.items()
                if name != "pause_sha256"
            }).encode()
        ).hexdigest()
        CONTROL.write(pause_path, changed)
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "emergency terminal evidence is invalid",
        ):
            controller.qualification_emergency_terminal(ticket)
        CONTROL.write(pause_path, pause)

        controller.product_ticket_done = lambda selected: selected == ticket
        controller.record_qualification_done_targets()
        controller.record_qualification_done_targets()
        records = [CONTROL.read(path) for path in controller.events.glob("*.json")]
        self.assertEqual(sum(
            item.get("event") == "emergency_terminal_reconciled"
            for item in records
        ), 1)
        self.assertEqual(sum(
            item.get("event") == "ticket_complete" for item in records
        ), 1)

    def test_qualification_release_receipts_follow_exact_chain(self) -> None:
        environment = self.root
        releases = environment / "releases"
        projects = environment / "projects"
        receipts = environment / "receipts"
        for path in (releases, projects, receipts, projects / "relay"):
            path.mkdir(mode=0o700)
        current = "a" * 40
        prior = "e" * 40
        release = releases / current
        release.mkdir()
        controller = CONTROL.Controller(self.args)
        controller.release_path = release

        def receipt(kit_sha: str, previous: str | None, project: str = "relay") -> str:
            value = {
                "contract_version": "1.8.0",
                "kit_sha": kit_sha,
                "kit_tree": "1" * 40,
                "product_path": str(self.product),
                "project": project,
                "provider_policy_sha256": "2" * 64,
                "qualification_mode": "isolated",
                "status": "pass",
            }
            if previous:
                value["previous_receipt_id"] = previous
            receipt_id = hashlib.sha256(
                (CONTROL.canonical(value) + "\n").encode()
            ).hexdigest()
            value["receipt_id"] = receipt_id
            CONTROL.write(receipts / f"{receipt_id}.json", value)
            return receipt_id

        prior_receipt = receipt(prior, None)
        current_receipt = receipt(current, prior_receipt)
        active_path = projects / "relay/active.json"
        CONTROL.write(active_path, {
            "kit_sha": current,
            "project": "relay",
            "receipt_id": current_receipt,
            "release_path": str(release),
        })
        self.assertEqual(controller.qualification_release_receipts(), {
            current: current_receipt,
            prior: prior_receipt,
        })

        changed = CONTROL.read(receipts / f"{prior_receipt}.json")
        changed["project"] = "other"
        CONTROL.write(receipts / f"{prior_receipt}.json", changed)
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "qualification release receipt is invalid",
        ):
            controller.qualification_release_receipts()

        foreign_receipt = receipt(prior, None, "other")
        current_receipt = receipt(current, foreign_receipt)
        CONTROL.write(active_path, {
            "kit_sha": current,
            "project": "relay",
            "receipt_id": current_receipt,
            "release_path": str(release),
        })
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "qualification release receipt is invalid",
        ):
            controller.qualification_release_receipts()

    def test_qualification_plain_done_without_protected_terminal_refuses(self) -> None:
        tickets = [f"T-{number}" for number in range(110, 114)]
        (self.product / "factory/QUALIFICATION.json").write_text(json.dumps({
            "budget_usd": "100.000000",
            "capacity": 4,
            "contract_version": "1.8.0",
            "factory_sha": "a" * 40,
            "generation": 1,
            "per_run_budget_usd": "2.000000",
            "per_ticket_budget_usd": "25.000000",
            "schema": CONTROL.QUALIFICATION_SCHEMA,
            "target_done": 4,
            "tickets": tickets,
        }), encoding="utf-8")
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.parent.mkdir()
        ticket.write_text("State: Done\n", encoding="utf-8")
        controller = CONTROL.Controller(self.args)
        with (
            patch.object(
                CONTROL, "protected_terminal",
                side_effect=CONTROL.ProtectedTerminalError("missing evidence"),
            ),
            self.assertRaisesRegex(
                CONTROL.ControllerError, "protected terminal is invalid",
            ),
        ):
            controller.record_qualification_done_targets()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        os.chmod(self.root, 0o700)
        self.product = self.root / "product"
        (self.product / "factory/runs").mkdir(parents=True)
        (self.product / "factory/PROJECT.env").write_text(
            "MAX_CONCURRENT_TICKETS=4\n", encoding="utf-8"
        )
        (self.product / "factory/ENVELOPE.env").write_text(
            "PER_TICKET_BUDGET_USD=25.000000\n", encoding="utf-8"
        )
        self.state = self.root / "controller"
        self.state.mkdir(mode=0o700)
        self.launcher = self.root / "factory-launch"
        self.launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        self.launcher.chmod(0o700)
        self.release = self.root / ("a" * 40)
        self.release.mkdir()
        self.args = argparse.Namespace(
            launcher=self.launcher,
            product_root=self.product,
            project="relay",
            release_path=self.release,
            state_dir=self.state,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def healthy_model_plan() -> dict:
        return {
            "profile_hash": "f" * 64,
            "profile_id": "cursor-opus-v1",
            "schema": "model-resolution-plan/v1",
            "selections": {
                role: {} for role in (
                    "planner", "builder", "narrator", "spec-linter",
                    "test-author", "reviewer",
                )
            },
        }

    @staticmethod
    def model_resolution_error(
        reason: str = "profile_resolution_failed",
        operation: str = "plan",
    ) -> dict:
        prefix = "model plan failed" if operation == "plan" else "model pin resolution failed"
        return {
            "error": f"{prefix}: {reason}",
            "profile_id": "cursor-opus-v1",
            "readiness": {
                "codex-gpt-5.6-sol": {
                    "adapter_version": "0.147.0",
                    "reason": (
                        "authentication_unavailable"
                        if reason == "profile_temporarily_unavailable"
                        else "version_mismatch"
                    ),
                    "reported_identity": "",
                    "state": (
                        "UNAVAILABLE"
                        if reason == "profile_temporarily_unavailable"
                        else "INVALID"
                    ),
                },
            },
            "reason_code": reason,
            "schema": CONTROL.MODEL_RESOLUTION_ERROR_SCHEMA,
            "status": "error",
        }

    def qualification_controller(self) -> CONTROL.Controller:
        (self.product / "factory/QUALIFICATION.json").write_text(
            json.dumps({
                "budget_usd": "100.000000",
                "capacity": 4,
                "contract_version": "1.8.0",
                "factory_sha": self.release.name,
                "generation": 1,
                "per_run_budget_usd": "2.000000",
                "per_ticket_budget_usd": "25.000000",
                "schema": CONTROL.QUALIFICATION_SCHEMA,
                "target_done": 4,
                "tickets": ["T-110", "T-111", "T-112", "T-113"],
            }),
            encoding="utf-8",
        )
        return CONTROL.Controller(self.args)

    @staticmethod
    def initialize_parked_branch(cell: Path, branch: str) -> None:
        cell.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "-q", "-b", branch, str(cell)], check=True,
        )

    def recovery_claim(self, ticket: str = "T-110") -> dict:
        cell = self.root / f"parked/{ticket}"
        self.initialize_parked_branch(cell, f"ticket/{ticket}")
        (cell / "tracked").write_text("base\n", encoding="utf-8")
        ticket_path = cell / "factory/tickets" / f"{ticket}.md"
        ticket_path.parent.mkdir(parents=True)
        ticket_path.write_text("State: Ready\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(cell), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(cell), "-c", "user.name=Factory",
            "-c", "user.email=factory@example.invalid", "commit", "-qm", "base",
        ], check=True)
        return {
            "branch": f"ticket/{ticket}", "blocked_reason": "retryable",
            "lease": ticket[-1] * 64, "parked": True, "priority": "normal",
            "publication_lease": "", "receipt": "", "role": "",
            "schema": CONTROL.CLAIM_SCHEMA, "status": "blocked",
            "ticket": ticket, "worktree": str(cell),
        }

    def semantic_wait_fixture(
        self, name: str, ticket: str = "T-110", *, duplicates: bool = False,
        role: str = "spec-linter", semantic_round: int = 3,
        semantic_kind: str = "planner-spec-linter",
        historical_controls: str = "", spec_controls: str | None = None,
    ) -> tuple[CONTROL.Controller, dict, Path, dict, dict]:
        cell = self.root / name
        remote = self.root / f"{name}.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        subprocess.run(
            ["git", "init", "-q", "-b", f"ticket/{ticket}", str(cell)],
            check=True,
        )
        ticket_path = cell / f"factory/tickets/{ticket}.md"
        route_path = cell / f"factory/route-plans/{ticket}.json"
        pin_path = cell / "factory/KIT_PIN"
        ticket_path.parent.mkdir(parents=True)
        route_path.parent.mkdir(parents=True)
        authorization = (
            f"OPERATOR AUTHORIZATION: {role} round {semantic_round}\n"
        )
        spec_failures = spec_controls if spec_controls is not None else (
            "".join(
                (
                    "" if index < 3 else
                    f"OPERATOR AUTHORIZATION: spec-linter round {index}\n"
                ) + f"SPEC-LINT: FAIL — {index}\n"
                for index in range(1, semantic_round)
            )
            if semantic_kind == "planner-spec-linter" else ""
        )
        ticket_path.write_text(
            f"# {ticket}\n\nState: Planning\nKit-SHA: {self.release.name}\n"
            + historical_controls
            + spec_failures
            + (authorization + authorization.rstrip("\n") if duplicates else ""),
            encoding="utf-8",
        )
        route_path.write_text(
            CONTROL.canonical({
                "kit_sha": self.release.name,
                "schema": "ticket-model-route-plan/v1",
                "ticket": ticket,
            }) + "\n",
            encoding="utf-8",
        )
        pin_path.write_text(self.release.name + "\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(cell), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(cell), "-c", "user.name=Test",
            "-c", "user.email=test@nysa.dev", "commit", "-qm", "wait",
        ], check=True)
        subprocess.run(
            ["git", "-C", str(cell), "remote", "add", "origin", str(remote)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(cell), "push", "-q", "-u", "origin", "HEAD"],
            check=True,
        )
        head = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD"], text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD^{tree}"], text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        route_digest = hashlib.sha256(route_path.read_bytes()).hexdigest()
        key = self.state / "passport.key"
        if not key.exists():
            key.write_bytes(b"k" * 32)
            key.chmod(0o600)
        passport = PASSPORT.authenticate({
            "base_history": [head], "branch": f"ticket/{ticket}",
            "charge_records": [], "completed_role_evidence": [],
            "contract_version": "1.8.0", "current_state": "Planning",
            "factory_release_history": [{
                "contract_version": "1.8.0", "factory_sha": self.release.name,
            }],
            "factory_sha": self.release.name, "head_sha": head,
            "head_tree": tree, "migration_history": [],
            "product_origin_sha256": hashlib.sha256(
                b"git@example.invalid:nysa/product.git"
            ).hexdigest(),
            "project": "relay",
            "protected_base_sha": head, "publication_state": "none",
            "route_plan_sha256": route_digest,
            "schema": "nysa.software-factory.ticket-passport/v1",
            "ticket": ticket,
            "ticket_blob": subprocess.run(
                ["git", "-C", str(cell), "rev-parse", f"HEAD:{ticket_path.relative_to(cell)}"],
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
            "transition_receipt_sha256": "",
        }, key.read_bytes())
        passports = self.state / "passports"
        passports.mkdir(mode=0o700, exist_ok=True)
        passport_path = passports / f"{ticket}.json"
        PASSPORT.write_atomic(passport_path, passport)
        stage = (
            "AWAIT-OPERATOR semantic-round authorization invalid; keep exactly "
            f"one line: OPERATOR AUTHORIZATION: {role} round {semantic_round}"
            if duplicates else
            "AWAIT-OPERATOR semantic-round authorization required; add exact "
            f"line: OPERATOR AUTHORIZATION: {role} round {semantic_round}"
        )
        transition = {
            "branch": f"ticket/{ticket}", "consumed": False,
            "contract_version": "1.8.0", "factory_sha": self.release.name,
            "head_sha": head,
            "loop": {
                "attempt": semantic_round - 1,
                "capped": semantic_round - 1 >= (
                    2 if semantic_kind == "narrator-bundle" else 3
                ),
                "kind": semantic_kind,
                "limit": 2 if semantic_kind == "narrator-bundle" else 3,
            },
            "passport_sha256": hashlib.sha256(passport_path.read_bytes()).hexdigest(),
            "project": "relay", "role": None,
            "route_plan_sha256": route_digest,
            "schema": "nysa.software-factory.transition-receipt/v1",
            "stage": stage, "ticket": ticket,
        }
        transition["receipt_sha256"] = hashlib.sha256(
            CONTROL.canonical_document({
                key: value for key, value in transition.items()
                if key not in {"consumed", "receipt_sha256"}
            })
        ).hexdigest()
        CONTROL.write(self.state / f"{ticket}.json", transition)
        claim = {
            "blocked_reason": CONTROL.semantic_block_reason(
                role, semantic_round,
            ),
            "branch": f"ticket/{ticket}", "lease": "2" * 64,
            "priority": "normal", "publication_lease": "", "receipt": "",
            "role": "", "schema": CONTROL.CLAIM_SCHEMA, "status": "waiting",
            "ticket": ticket, "worktree": str(cell),
        }
        controller = CONTROL.Controller(self.args)
        controller.worktrees_by_branch = lambda: {
            f"refs/heads/ticket/{ticket}": [str(cell)],
        }
        controller.save_claim(claim)
        return controller, claim, cell, passport, transition

    def reviewer_void_fixture(
        self, name: str, ticket: str, *, controls: str = "reviewer round 1: APPROVE\n",
    ) -> tuple[CONTROL.Controller, dict, Path, dict, dict]:
        cell = self.root / "parked" / ticket
        remote = self.root / f"{name}.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        subprocess.run(
            ["git", "init", "-q", "-b", f"ticket/{ticket}", str(cell)],
            check=True,
        )
        ticket_path = cell / f"factory/tickets/{ticket}.md"
        route_path = cell / f"factory/route-plans/{ticket}.json"
        ticket_path.parent.mkdir(parents=True)
        route_path.parent.mkdir(parents=True)
        ticket_path.write_text(
            f"# {ticket}\n\nState: Review\nKit-SHA: {self.release.name}\n"
            + controls,
            encoding="utf-8",
        )
        route_path.write_text(
            CONTROL.canonical({
                "kit_sha": self.release.name,
                "schema": "ticket-model-route-plan/v1", "ticket": ticket,
            }) + "\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(cell), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(cell), "-c", "user.name=Test",
            "-c", "user.email=test@nysa.dev", "commit", "-qm", "review wait",
        ], check=True)
        subprocess.run(
            ["git", "-C", str(cell), "remote", "add", "origin", str(remote)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(cell), "push", "-q", "-u", "origin", "HEAD"],
            check=True,
        )
        head = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD"], text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD^{tree}"], text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        route_digest = hashlib.sha256(route_path.read_bytes()).hexdigest()
        key = self.state / "passport.key"
        if not key.exists():
            key.write_bytes(b"k" * 32)
            key.chmod(0o600)
        completed = [{
            "contract_version": "1.8.0", "factory_sha": self.release.name,
            "head_before": head,
            "manifest_sha256": hashlib.sha256(f"manifest-{number}".encode()).hexdigest(),
            "output_sha256": hashlib.sha256(f"output-{number}".encode()).hexdigest(),
            "role": "reviewer", "run_id": f"reviewer-{number}",
            "transition_receipt_sha256": hashlib.sha256(
                f"receipt-{number}".encode()
            ).hexdigest(),
        } for number in (1, 2)]
        passport = PASSPORT.authenticate({
            "base_history": [head], "branch": f"ticket/{ticket}",
            "charge_records": [], "completed_role_evidence": completed,
            "contract_version": "1.8.0", "current_state": "Review",
            "factory_release_history": [{
                "contract_version": "1.8.0", "factory_sha": self.release.name,
            }],
            "factory_sha": self.release.name, "head_sha": head,
            "head_tree": tree, "migration_history": [],
            "product_origin_sha256": hashlib.sha256(
                b"git@example.invalid:nysa/product.git"
            ).hexdigest(),
            "project": "relay", "protected_base_sha": head,
            "publication_state": "none", "route_plan_sha256": route_digest,
            "schema": "nysa.software-factory.ticket-passport/v1",
            "ticket": ticket,
            "ticket_blob": subprocess.run(
                ["git", "-C", str(cell), "rev-parse",
                 f"HEAD:{ticket_path.relative_to(cell)}"],
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
            "transition_receipt_sha256": "",
        }, key.read_bytes())
        passports = self.state / "passports"
        passports.mkdir(mode=0o700, exist_ok=True)
        passport_path = passports / f"{ticket}.json"
        PASSPORT.write_atomic(passport_path, passport)
        historical_lease = "3" * 64
        transition = {
            "branch": f"ticket/{ticket}", "consumed": False,
            "contract_version": "1.8.0", "factory_sha": self.release.name,
            "head_sha": head,
            "lease_sha256": hashlib.sha256(historical_lease.encode()).hexdigest(),
            "loop": None,
            "passport_sha256": hashlib.sha256(passport_path.read_bytes()).hexdigest(),
            "project": "relay", "role": None,
            "route_plan_sha256": route_digest,
            "schema": "nysa.software-factory.transition-receipt/v1",
            "stage": (
                "REFUSE reviewer has 2 non-void successful run(s) but only 1 "
                f"verdict(s) are logged on {ticket_path} — record the missing "
                "verdict, or mark a duplicate successful row with 'OPERATOR "
                "NOTE: reviewer run <ledger ordinal> void — duplicate'"
            ),
            "ticket": ticket,
        }
        transition["receipt_sha256"] = hashlib.sha256(
            CONTROL.canonical_document({
                key: value for key, value in transition.items()
                if key != "consumed"
            })
        ).hexdigest()
        CONTROL.write(self.state / f"{ticket}.json", transition)
        claim = {
            "blocked_reason": "state-machine-refusal",
            "branch": f"ticket/{ticket}", "lease": "", "parked": True,
            "priority": "normal", "publication_lease": "", "receipt": "",
            "role": "", "schema": CONTROL.CLAIM_SCHEMA, "status": "blocked",
            "ticket": ticket, "worktree": str(cell),
        }
        controller = CONTROL.Controller(self.args)
        controller.worktrees_by_branch = lambda: {
            f"refs/heads/ticket/{ticket}": [str(cell)],
        }
        controller.save_claim(claim)
        return controller, claim, cell, passport, transition

    def migrate_semantic_wait_passport(
        self, controller: CONTROL.Controller, claim: dict,
    ) -> dict:
        path = self.state / f"passports/{claim['ticket']}.json"
        before = controller.authenticated_operator_passport(claim["ticket"])
        assert before is not None
        parent_file = hashlib.sha256(path.read_bytes()).hexdigest()
        head = subprocess.run(
            ["git", "-C", claim["worktree"], "rev-parse", "HEAD"], text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "-C", claim["worktree"], "rev-parse", "HEAD^{tree}"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        edge = {
            "from_factory_sha": before["factory_sha"],
            "from_head_sha": before["head_sha"],
            "from_passport_file_sha256": parent_file,
            "from_passport_sha256": before["passport_sha256"],
            "from_protected_base_sha": before["protected_base_sha"],
            "from_route_plan_sha256": before["route_plan_sha256"],
            "schema": PASSPORT.MIGRATION_SCHEMA,
            "to_factory_sha": before["factory_sha"], "to_head_sha": head,
            "to_protected_base_sha": before["protected_base_sha"],
            "to_route_plan_sha256": before["route_plan_sha256"],
        }
        migrated = PASSPORT.authenticate({
            **{
                key: value for key, value in before.items()
                if key not in {
                    "authentication_sha256", "passport_sha256",
                    "parent_digest", "parent_file_sha256",
                }
            },
            "head_sha": head, "head_tree": tree,
            "ticket_blob": subprocess.run(
                [
                    "git", "-C", claim["worktree"], "rev-parse",
                    f"HEAD:factory/tickets/{claim['ticket']}.md",
                ],
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
            "migration_history": [*before["migration_history"], edge],
            "parent_digest": before["passport_sha256"],
            "parent_file_sha256": parent_file,
        }, (self.state / "passport.key").read_bytes())
        PASSPORT.write_atomic(path, migrated)
        return migrated

    def validate_semantic_passport(self, claim: dict) -> dict:
        with patch.dict(
            os.environ,
            {"FACTORY_CERTIFIED_PRODUCT_ORIGIN":
             "git@example.invalid:nysa/product.git"},
        ):
            return PASSPORT.validate(argparse.Namespace(
                project="relay", state_dir=self.state,
                ticket=claim["ticket"], workdir=Path(claim["worktree"]),
            ), (self.state / "passport.key").read_bytes())

    def initialize_passportless_planner_claims(
        self, tickets: list[str]
    ) -> tuple[CONTROL.Controller, list[dict]]:
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.product)], check=True,
        )
        for key, value in (
            ("user.name", "Software Factory"),
            ("user.email", "factory@local"),
        ):
            subprocess.run(
                ["git", "-C", str(self.product), "config", key, value], check=True,
            )
        ticket_dir = self.product / "factory/tickets"
        ticket_dir.mkdir()
        (self.product / "factory/KIT_PIN").write_text(
            self.release.name + "\n", encoding="utf-8",
        )
        for ticket in tickets:
            (ticket_dir / f"{ticket}.md").write_text(
                f"# {ticket}\n\nState: Ready\n", encoding="utf-8",
            )
        subprocess.run(
            ["git", "-C", str(self.product), "add", "factory"], check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.product), "commit", "-qm", "baseline"],
            check=True,
        )
        remote = self.root / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        subprocess.run(
            ["git", "-C", str(self.product), "remote", "add", "origin", str(remote)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.product), "push", "-q", "origin", "main"],
            check=True,
        )
        claims = []
        catalog, routes, _profiles, profile_map = ROUTER.load_policy()
        readiness = {
            route_id: {
                "adapter_version": "test-v1", "reason": "ok",
                "reported_identity": value["expected_reported_identity"],
                "state": "READY",
            }
            for route_id, value in routes.items() if value["enabled"]
        }
        resolution = ROUTER.resolve_policy(
            catalog, routes, profile_map["cursor-opus-v1"], readiness,
        )
        for number, ticket in enumerate(tickets, 1):
            worktree = self.root / f"cell-{number}"
            branch = f"ticket/{ticket}"
            subprocess.run(
                [
                    "git", "-C", str(self.product), "worktree", "add", "-q",
                    "-b", branch, str(worktree), "main",
                ],
                check=True,
            )
            ticket_path = worktree / f"factory/tickets/{ticket}.md"
            ticket_path.write_text(
                f"# {ticket}\n\nState: Planning\nKit-SHA: {self.release.name}\n",
                encoding="utf-8",
            )
            route_path = worktree / f"factory/route-plans/{ticket}.json"
            route_path.parent.mkdir()
            route_path.write_text(ROUTER.canonical_json({
                "created_at": "2026-08-07T00:00:00Z",
                "kit_sha": self.release.name,
                "resolution": resolution,
                "schema": "ticket-model-route-plan/v1",
                "ticket": ticket,
            }) + "\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(worktree), "add", "factory"], check=True,
            )
            subprocess.run(
                [
                    "git", "-C", str(worktree), "commit", "-qm",
                    f"{ticket}: pre-provider controls",
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(worktree), "push", "-qu", "origin", branch],
                check=True,
            )
            head = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                text=True, capture_output=True, check=True,
            ).stdout.strip()
            tree = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD^{tree}"],
                text=True, capture_output=True, check=True,
            ).stdout.strip()
            ticket_blob = subprocess.run(
                [
                    "git", "-C", str(worktree), "rev-parse",
                    f"HEAD:factory/tickets/{ticket}.md",
                ],
                text=True, capture_output=True, check=True,
            ).stdout.strip()
            lease = hashlib.sha256(f"lease-{ticket}".encode()).hexdigest()
            route_raw = route_path.read_bytes()
            receipt = {
                "branch": branch,
                "consumed": False,
                "contract_version": "1.8.0",
                "factory_sha": self.release.name,
                "head_sha": head,
                "head_tree": tree,
                "lease_sha256": hashlib.sha256(lease.encode()).hexdigest(),
                "passport_sha256": None,
                "product_origin_sha256": hashlib.sha256(
                    str(remote).encode()
                ).hexdigest(),
                "project": "relay",
                "role": "planner",
                "route_plan_sha256": hashlib.sha256(route_raw).hexdigest(),
                "schema": "nysa.software-factory.transition-receipt/v1",
                "stage": "RUN planner",
                "ticket": ticket,
                "ticket_blob": ticket_blob,
            }
            receipt["receipt_sha256"] = hashlib.sha256(STATE.canonical({
                key: value for key, value in receipt.items()
                if key not in {"consumed", "receipt_sha256"}
            })).hexdigest()
            CONTROL.write(self.state / f"{ticket}.json", receipt)
            claim = {
                "blocked_reason": "worker-error",
                "branch": branch,
                "lease": lease,
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "blocked",
                "ticket": ticket,
                "worktree": str(worktree),
            }
            if number == len(tickets):
                claim["lease_released"] = True
            claims.append(claim)
        return CONTROL.Controller(self.args), claims

    def install_passportless_fallback(
        self, claim: dict, snapshot_path: str | None = None,
    ) -> dict:
        worktree = Path(claim["worktree"])
        ticket = claim["ticket"]
        receipt_path = self.state / f"{ticket}.json"
        receipt = CONTROL.read(receipt_path)
        route_path = worktree / f"factory/route-plans/{ticket}.json"
        catalog, routes, _profiles, profile_map = ROUTER.load_policy()
        journal = MANAGER.migrate_v1_plan(
            route_path.read_bytes(), receipt["head_sha"], self.release.name,
            "2026-08-07T00:00:30Z", catalog, routes, profile_map,
        )
        prior = MANAGER.active_resolution(journal)
        failed = prior["selections"]["planner"]
        readiness = {
            route_id: {
                "adapter_version": "test-v1", "reason": "ok",
                "reported_identity": value["expected_reported_identity"],
                "state": "READY",
            }
            for route_id, value in routes.items() if value["enabled"]
        }
        readiness[failed["route_id"]].update(
            reason="provider_unavailable", state="UNAVAILABLE",
        )
        fallback = ROUTER.resolve_fallback_policy(
            catalog, routes, profile_map[prior["profile_id"]], readiness,
            prior, "planner", failed["route_id"], ["planner"],
            {"P": [], "T": [], "B": []},
        )
        run_id = f"fallback-{ticket}"
        terminal = (
            f"run_id={run_id}\nphase=completed\n"
            "accounting_state=abandoned_conservative\n"
            "go_issued=1\ntask_submitted=1\nexit_status=9\n"
            f"ticket={ticket}\nrole=planner\nadapter={failed['adapter']}\n"
            f"provider_family={failed['provider_family']}\n"
            f"model_id={failed['selection_id']}\nroute_id={failed['route_id']}\n"
            f"policy_hash={prior['policy_hash']}\nrole_exit=provider_failed\n"
            f"role_branch_before={claim['branch']}\n"
            f"role_head_before={receipt['head_sha']}\n"
            f"role_remote_before={receipt['head_sha']}\n"
            f"kit_sha={self.release.name}\n"
        ).encode()
        (self.product / f"factory/runs/{run_id}.meta").write_bytes(terminal)
        approval = {
            "approval_hash": "1" * 64,
            "failed_run_id": run_id,
            "generation": 1,
            "manifest_digest": "2" * 64,
            "nonce": "3" * 32,
            "schema": "ticket-model-fallback-qualification/v1",
        }
        entries = ()
        if snapshot_path:
            content = b"untrusted sibling mutation\n"
            candidate = worktree / snapshot_path
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_bytes(content)
            blob = subprocess.run(
                ["git", "-C", worktree, "hash-object", "--stdin"],
                input=content, capture_output=True, check=True,
            ).stdout.decode().strip()
            entries = (HANDOFF.SnapshotEntry(
                path=snapshot_path, state="file", mode="100644", blob_oid=blob,
                content_sha256=hashlib.sha256(content).hexdigest(), size=len(content),
            ),)
        snapshot_digest = HANDOFF._snapshot_digest(entries)
        journal = MANAGER.append_fallback_revision(
            journal, fallback, hashlib.sha256(terminal).hexdigest(), snapshot_digest,
            "provider_unavailable", approval, "2026-08-07T00:00:45Z",
            catalog, routes, profile_map,
        )
        route_path.write_text(ROUTER.canonical_json(journal) + "\n")
        subprocess.run(["git", "-C", worktree, "add", "-A"], check=True)
        revision = journal["revisions"][-1]["revision_hash"]
        subprocess.run(
            ["git", "-C", worktree, "commit", "-qm",
             f"{ticket}: preserve failed attempt and revise model route",
             "-m", "Failed-Attempt-Snapshot: " + snapshot_digest,
             "-m", "Model-Route-Revision: " + revision],
            check=True,
        )
        subprocess.run(
            ["git", "-C", worktree, "push", "-q", "origin", claim["branch"]],
            check=True,
        )
        head = subprocess.run(
            ["git", "-C", worktree, "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        receipt.update(
            head_sha=head,
            head_tree=subprocess.run(
                ["git", "-C", worktree, "rev-parse", "HEAD^{tree}"],
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
            route_plan_sha256=hashlib.sha256(route_path.read_bytes()).hexdigest(),
            ticket_blob=subprocess.run(
                ["git", "-C", worktree, "rev-parse",
                 f"HEAD:factory/tickets/{ticket}.md"],
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
        )
        immutable = {
            key: value for key, value in receipt.items()
            if key not in {"consumed", "consumed_at_epoch", "receipt_sha256"}
        }
        receipt["receipt_sha256"] = hashlib.sha256(
            STATE.canonical(immutable)
        ).hexdigest()
        receipt_path.unlink()
        CONTROL.write(receipt_path, receipt)
        return journal

    def test_qualification_claim_rechecks_fallback_readiness(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {"tickets": ["T-110"]}
        controller.qualification_fallback_readiness_sha256 = "f" * 64
        calls = []

        def ready(*args, **_kwargs):
            calls.append(args)
            if args[:2] == ("models", "qualification-readiness"):
                return {
                    "readiness_sha256": "f" * 64,
                    "schema": "nysa.software-factory.qualification-fallback-readiness/v1",
                    "status": "ready",
                }
            return {"action": "WAIT"}

        controller.json_call = ready
        self.assertEqual(controller.claim_new([]), [])
        self.assertEqual(calls[0], ("models", "qualification-readiness", "--json"))
        controller.json_call = lambda *_args, **_kwargs: {
            "readiness_sha256": "f" * 64,
            "schema": "nysa.software-factory.qualification-fallback-readiness/v1",
            "status": "invalid",
        }
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "fallback readiness drifted",
        ):
            controller.claim_new([])
        controller.json_call = lambda *_args, **_kwargs: {
            "readiness_sha256": "e" * 64,
            "schema": "nysa.software-factory.qualification-fallback-readiness/v1",
            "status": "ready",
        }
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "fallback readiness drifted",
        ):
            controller.claim_new([])

    def test_qualification_preflight_blocks_before_recovery_once(self) -> None:
        controller = self.qualification_controller()
        calls = []

        def preflight(*arguments, **kwargs):
            calls.append((arguments, kwargs))
            return {
                "action": "ESCALATE",
                "error": (
                    "selected-ticket operator projection is invalid: T-110"
                ),
                "reason_code": "unsafe_state",
                "schema": "nysa.software-factory.dispatch-plan/v1",
                "status": "error",
            }

        controller.json_call = preflight
        controller.cancellation_authority = lambda _claims: (
            (_ for _ in ()).throw(AssertionError("recovery started"))
        )

        first = controller.reconcile()
        second = controller.reconcile()

        expected = {
            "error": "qualification admission preflight failed",
            "reason_code": "qualification_admission_preflight_failed",
            "status": "error",
        }
        self.assertEqual(first["results"], [expected])
        self.assertEqual(second["results"], [expected])
        self.assertEqual(first["active"], 0)
        self.assertEqual(first["status"], "error")
        self.assertEqual(
            calls[0],
            (("dispatch-plan", "--shadow", "--json"), {"allow": (0, 2)}),
        )
        incident = CONTROL.read(self.state / "admission-incident.json")
        self.assertEqual(incident["count"], 2)
        self.assertEqual(
            incident["reason_code"], "qualification_admission_preflight_failed",
        )
        events = [
            CONTROL.read(path) for path in controller.events.glob("*.json")
            if CONTROL.read(path).get("event") == "admission_blocked"
        ]
        self.assertEqual(len(events), 1)

    def test_accounted_qualification_cohort_skips_only_new_admission(self) -> None:
        controller = self.qualification_controller()
        tickets = controller.qualification["tickets"]
        claims = [{"ticket": ticket} for ticket in tickets]
        controller.json_call = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(AssertionError("new admission ran"))
        )

        self.assertIsNone(controller.qualification_admission_preflight(claims))
        self.assertEqual(controller.claim_new(claims), claims)

        controller.product_ticket_done = lambda ticket: ticket == tickets[-1]
        self.assertIsNone(
            controller.qualification_admission_preflight(claims[:-1])
        )
        self.assertEqual(controller.claim_new(claims[:-1]), claims[:-1])
        controller.product_ticket_done = lambda _ticket: False

        calls = []
        controller.json_call = lambda *args, **_kwargs: (
            calls.append(args) or {
                "action": "WAIT",
                "schema": "nysa.software-factory.dispatch-plan/v1",
                "status": "WAIT",
            }
        )
        self.assertIsNone(
            controller.qualification_admission_preflight(claims[:-1])
        )
        self.assertIsNone(controller.qualification_admission_preflight(
            [*claims, {"ticket": "T-999"}],
        ))
        self.assertEqual(
            calls,
            [
                ("dispatch-plan", "--shadow", "--json"),
                ("dispatch-plan", "--shadow", "--json"),
            ],
        )

    def test_qualification_preflight_accepts_shadow_and_wait(self) -> None:
        class ExistingFlowReached(Exception):
            pass

        for value in (
            {
                "action": "SHADOW",
                "schema": "nysa.software-factory.dispatch-plan/v1",
                "status": "SHADOW",
                "ticket": "T-110",
            },
            {
                "action": "WAIT",
                "reason_code": "no_candidate",
                "schema": "nysa.software-factory.dispatch-plan/v1",
                "status": "WAIT",
            },
        ):
            with self.subTest(action=value["action"]):
                controller = self.qualification_controller()
                controller.json_call = lambda *_args, **_kwargs: value
                controller.cancellation_authority = lambda _claims: (
                    (_ for _ in ()).throw(ExistingFlowReached)
                )
                with self.assertRaises(ExistingFlowReached):
                    controller.reconcile()

    def test_production_reconcile_skips_qualification_preflight(self) -> None:
        class ExistingFlowReached(Exception):
            pass

        controller = CONTROL.Controller(self.args)
        controller.json_call = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(AssertionError("qualification preflight ran"))
        )
        controller.cancellation_authority = lambda _claims: (
            (_ for _ in ()).throw(ExistingFlowReached)
        )

        with self.assertRaises(ExistingFlowReached):
            controller.reconcile()

    def test_qualification_preflight_rejects_malformed_status_pair(self) -> None:
        controller = self.qualification_controller()
        controller.json_call = lambda *_args, **_kwargs: {
            "action": "WAIT",
            "schema": "nysa.software-factory.dispatch-plan/v1",
            "status": "SHADOW",
        }

        failure = controller.qualification_admission_preflight([])

        self.assertEqual(failure, {
            "error": "qualification admission preflight failed",
            "reason_code": "qualification_admission_preflight_failed",
            "status": "error",
        })
        self.assertEqual(
            CONTROL.read(self.state / "admission-incident.json")["reason_code"],
            "qualification_admission_preflight_failed",
        )

    def test_invalid_model_profile_blocks_before_real_claim(self) -> None:
        controller = CONTROL.Controller(self.args)
        calls = []

        def admission(*args, **_kwargs):
            calls.append(args)
            if args[:2] == ("dispatch-plan", "--shadow"):
                return {"action": "SHADOW", "ticket": "T-110"}
            if args[:2] == ("models", "plan"):
                return self.model_resolution_error()
            raise AssertionError("real claim must not run with an invalid profile")

        controller.json_call = admission
        with self.assertRaises(CONTROL.ControllerError) as raised:
            controller.claim_new([])

        failure = json.loads(str(raised.exception))
        self.assertEqual(failure["ticket"], "T-110")
        self.assertEqual(failure["reason_code"], "profile_resolution_failed")
        self.assertEqual(
            [call[:2] for call in calls],
            [("dispatch-plan", "--shadow"), ("models", "plan")],
        )
        controller.record_admission_failure(raised.exception, [])
        incident = CONTROL.read(self.state / "admission-incident.json")
        self.assertEqual(incident["ticket"], "T-110")
        self.assertEqual(
            incident["readiness"]["codex-gpt-5.6-sol"]["reason"],
            "version_mismatch",
        )

    def test_repository_test_runs_one_mock_planner_without_real_model_access(self) -> None:
        (self.product / "factory/PROJECT.env").write_text(
            "MAX_CONCURRENT_TICKETS=3\n", encoding="utf-8",
        )
        authority = {
            "FACTORY_ADAPTER_OVERRIDE": "mock",
            "FACTORY_KIT_TRUST_SCOPE": "repository-test",
            "FACTORY_TEST_MODE": "1",
            "FACTORY_TRUSTED_TEST_HARNESS": "1",
        }
        with patch.dict(os.environ, authority, clear=False):
            controller = CONTROL.Controller(self.args)
        worktree = self.root / "worktree"
        ticket_path = worktree / "factory/tickets/T-110.md"
        ticket_path.parent.mkdir(parents=True)
        ticket_path.write_text("State: Ready\n", encoding="utf-8")
        subprocess.run(
            ["git", "init", "-q", "-b", "ticket/T-110", str(worktree)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(worktree), "add", "."], check=True,
        )
        subprocess.run(
            [
                "git", "-C", str(worktree),
                "-c", "user.name=Software Factory",
                "-c", "user.email=factory@local",
                "commit", "-qm", "seed Ready ticket",
            ],
            check=True,
        )
        ready_head = subprocess.check_output(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"], text=True,
        ).strip()
        wrong_lease = False
        transition_receipt_overrides: dict[str, object] = {}
        transition_response_overrides: dict[str, object] = {}
        transition_state = "Planning"
        transition_consumed = False
        commit_transition = True
        persist_transition = True
        terminal_overrides: dict[str, str] = {}
        planner_commits = True
        planner_non_descendant = False
        leave_transition_unconsumed = False
        claim_residue = ""
        run_ordinal = 0
        transition_ordinal = 0
        calls = []

        def admission(*args, **_kwargs):
            nonlocal transition_ordinal
            calls.append(args)
            if args[:2] == ("dispatch-plan", "--shadow"):
                return {"action": "SHADOW", "ticket": "T-110"}
            if args[:2] == ("dispatch-plan", "--claim"):
                return {
                    "action": "START",
                    "branch": "ticket/T-110",
                    "lease_id": "b" * 64,
                    "ticket": "T-110",
                    "worktree": str(worktree),
                }
            if args[0] == "renew":
                return {}
            if args[0] == "release":
                return {}
            if args[:2] == ("publication", "withdraw"):
                return {"status": "absent"}
            if args[0] == "state-machine":
                transition_ordinal += 1
                ticket_path.write_text(
                    f"State: {transition_state}\n", encoding="utf-8",
                )
                if commit_transition:
                    subprocess.run(
                        ["git", "-C", str(worktree), "add", str(ticket_path)],
                        check=True,
                    )
                    subprocess.run(
                        [
                            "git", "-C", str(worktree),
                            "-c", "user.name=Software Factory",
                            "-c", "user.email=factory@local",
                            "commit", "--allow-empty", "-qm",
                            "T-110: transition ticket state",
                        ],
                        check=True,
                    )
                head = subprocess.check_output(
                    ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                    text=True,
                ).strip()
                receipt = {
                    "branch": "ticket/T-110",
                    "contract_version": "2.0.0",
                    "evidence_sha256": "c" * 64,
                    "factory_sha": self.release.name,
                    "head_sha": head,
                    "head_tree": subprocess.check_output(
                        ["git", "-C", str(worktree), "rev-parse", "HEAD^{tree}"],
                        text=True,
                    ).strip(),
                    "lease_sha256": (
                        "0" * 64
                        if wrong_lease else
                        hashlib.sha256(("b" * 64).encode()).hexdigest()
                    ),
                    "loop": None,
                    "nonce": f"{transition_ordinal:032x}",
                    "passport_sha256": None,
                    "product_origin_sha256": "e" * 64,
                    "project": "relay",
                    "role": "planner",
                    "route_plan_sha256": None,
                    "schema": "nysa.software-factory.transition-receipt/v1",
                    "stage": "RUN planner",
                    "ticket": "T-110",
                    "ticket_blob": subprocess.check_output(
                        [
                            "git", "-C", str(worktree), "rev-parse",
                            "HEAD:factory/tickets/T-110.md",
                        ],
                        text=True,
                    ).strip(),
                }
                receipt.update(transition_receipt_overrides)
                digest = hashlib.sha256(
                    CONTROL.canonical_document(receipt)
                ).hexdigest()
                receipt.update(
                    receipt_sha256=digest, consumed=transition_consumed,
                )
                if transition_consumed:
                    receipt["consumed_at_epoch"] = 1
                if persist_transition:
                    CONTROL.write(self.state / "T-110.json", receipt)
                response = state_transition("RUN planner", receipt=digest)
                response.update(transition_response_overrides)
                return response
            raise AssertionError(
                f"repository-test consulted forbidden command: {args[:2]}"
            )

        def run_planner(instance, claim, role, receipt, failed_checks):
            nonlocal run_ordinal
            self.assertEqual((role, failed_checks), ("planner", []))
            before = subprocess.check_output(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"], text=True,
            ).strip()
            claim.update(receipt=receipt, role=role, status="running")
            instance.save_claim(claim)
            instance.event(
                "attempt_started", claim["ticket"], role=role,
                transition_receipt_sha256=receipt,
            )
            persisted = CONTROL.read(self.state / "T-110.json")
            persisted.update(consumed=True, consumed_at_epoch=1)
            CONTROL.write(self.state / "T-110.json", persisted)
            if planner_non_descendant:
                subprocess.run(
                    ["git", "-C", str(worktree), "reset", "--hard", ready_head],
                    check=True, stdout=subprocess.DEVNULL,
                )
            if planner_commits:
                subprocess.run(
                    [
                        "git", "-C", str(worktree),
                        "-c", "user.name=Software Factory",
                        "-c", "user.email=factory@local",
                        "commit", "--allow-empty", "-qm", "Mock planner output",
                    ],
                    check=True,
                )
            run_ordinal += 1
            run_id = f"repository-test-{run_ordinal}"
            output = self.product / "factory/runs" / f"{run_id}.out"
            output.parent.mkdir(exist_ok=True)
            output.write_text("mock adapter ran task\n", encoding="utf-8")
            output.chmod(0o600)
            manifest = output.with_suffix(".meta")
            values = {
                "run_id": run_id,
                "phase": "completed",
                "accounting_state": "completed",
                "go_issued": "1",
                "task_submitted": "1",
                "exit_status": "0",
                "role_exit": "ok",
                "ticket": "T-110",
                "role": "planner",
                "adapter": "mock",
                "selection_reason": "test_override",
                "kit_sha": self.release.name,
                "kit_provenance_scope": "repository-test",
                "role_head_before": before,
                "transition_receipt_sha256": receipt,
                "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            }
            values.update(terminal_overrides)
            manifest.write_text(
                "\n".join(f"{key}={value}" for key, value in values.items())
                + "\n", encoding="utf-8",
            )
            instance.finish_pending_run(claim)
            if leave_transition_unconsumed:
                persisted = CONTROL.read(self.state / "T-110.json")
                persisted.update(consumed=False)
                persisted.pop("consumed_at_epoch", None)
                CONTROL.write(self.state / "T-110.json", persisted)
            if claim_residue:
                claim[claim_residue] = {
                    "receipt": receipt,
                    "role": role,
                    "status": "running",
                }[claim_residue]
                instance.save_claim(claim)

        controller.json_call = admission
        controller.refresh_dependency_tracking = lambda _claim: True
        controller.run_role = lambda *args: run_planner(controller, *args)
        result = controller.reconcile()
        self.assertEqual([call[:2] for call in calls], [
            ("dispatch-plan", "--shadow"),
            ("dispatch-plan", "--claim"),
            ("renew", "--ticket"),
            ("state-machine", "--ticket"),
        ])
        self.assertEqual(
            result["results"],
            [{"status": "planner-complete", "ticket": "T-110"}],
        )
        self.assertEqual(ticket_path.read_text(), "State: Planning\n")
        claims = controller.load_claims()
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["status"], "claimed")
        self.assertEqual((claims[0]["receipt"], claims[0]["role"]), ("", ""))
        events = sorted(
            (CONTROL.read(path) for path in controller.events.glob("*.json")),
            key=lambda item: item["observed_at_epoch_ns"],
        )
        self.assertEqual(
            [item["event"] for item in events if item["ticket"] == "T-110"],
            [
                "ticket_claimed", "repository_test_planning",
                "attempt_started", "attempt_terminal",
                "repository_test_planner_completed",
            ],
        )
        for event in events:
            unsigned = dict(event)
            digest = unsigned.pop("event_sha256")
            self.assertEqual(
                digest,
                hashlib.sha256(CONTROL.canonical(unsigned).encode()).hexdigest(),
            )
        with self.assertRaisesRegex(
            CONTROL.ControllerError,
            "repository-test Planning canary requires empty execution state",
        ):
            controller.reconcile()

        (controller.claims / "T-110.json").unlink()
        (self.state / "T-110.json").unlink()
        subprocess.run(
            ["git", "-C", str(worktree), "reset", "--hard", ready_head],
            check=True, stdout=subprocess.DEVNULL,
        )
        calls.clear()
        wrong_lease = True
        planning_events = len([
            item for item in events if item["event"] == "repository_test_planning"
        ])
        with patch.dict(os.environ, authority, clear=False):
            tampered = CONTROL.Controller(self.args)
        tampered.json_call = admission
        tampered.refresh_dependency_tracking = lambda _claim: True
        tampered_result = tampered.reconcile()
        self.assertEqual(tampered_result["status"], "error")
        self.assertIn(
            "repository-test did not reach authenticated Planning",
            tampered_result["results"][0]["error"],
        )
        self.assertEqual(
            len([
                CONTROL.read(path)
                for path in tampered.events.glob("*.json")
                if CONTROL.read(path)["event"] == "repository_test_planning"
            ]),
            planning_events,
        )

        invalid_response = "state-machine returned invalid transition evidence"
        invalid_planning = "repository-test did not reach authenticated Planning"
        transition_cases = (
            (
                "response-stage", {"stage": "FIX planner"},
                {"action": "FIX", "stage": "FIX planner"},
                True, True, invalid_planning,
            ),
            ("response-role", {}, {"role": "builder"}, True, True, invalid_response),
            (
                "response-loop", {}, {"loop": {"kind": "unexpected"}},
                True, True, invalid_response,
            ),
            (
                "response-receipt", {}, {"receipt": "0" * 64},
                True, True, invalid_planning,
            ),
            (
                "persisted-stage", {"stage": "RUN builder"}, {},
                True, True, invalid_planning,
            ),
            (
                "persisted-role", {"role": "builder"}, {},
                True, True, invalid_planning,
            ),
            (
                "persisted-head", {"head_sha": "0" * 40}, {},
                True, True, invalid_planning,
            ),
            ("missing-receipt", {}, {}, True, False, invalid_planning),
            ("unchanged-head", {}, {}, False, True, invalid_planning),
            ("wrong-state", {}, {}, True, True, invalid_planning),
            ("consumed-receipt", {}, {}, True, True, invalid_planning),
        )
        for (
            label, receipt_changes, response_changes,
            should_commit, should_persist, expected_error,
        ) in transition_cases:
            (controller.claims / "T-110.json").unlink(missing_ok=True)
            (self.state / "T-110.json").unlink(missing_ok=True)
            subprocess.run(
                ["git", "-C", str(worktree), "reset", "--hard", ready_head],
                check=True, stdout=subprocess.DEVNULL,
            )
            calls.clear()
            wrong_lease = False
            transition_receipt_overrides = receipt_changes
            transition_response_overrides = response_changes
            transition_state = "Ready" if label == "wrong-state" else "Planning"
            transition_consumed = label == "consumed-receipt"
            commit_transition = should_commit
            persist_transition = should_persist
            with self.subTest(transition_evidence=label), patch.dict(
                os.environ, authority, clear=False,
            ):
                invalid_transition = CONTROL.Controller(self.args)
                invalid_transition.json_call = admission
                invalid_transition.refresh_dependency_tracking = lambda _claim: True
                invalid_transition.run_role = lambda *_args: self.fail(
                    "invalid Planning evidence reached planner launch"
                )
                invalid_result = invalid_transition.reconcile()
            self.assertEqual(invalid_result["status"], "error")
            self.assertIn(
                expected_error,
                invalid_result["results"][0]["error"],
            )
            self.assertEqual(
                len([
                    CONTROL.read(path)
                    for path in invalid_transition.events.glob("*.json")
                    if CONTROL.read(path)["event"] == "repository_test_planning"
                ]),
                planning_events,
            )

        (controller.claims / "T-110.json").unlink()
        (self.state / "T-110.json").unlink()
        subprocess.run(
            ["git", "-C", str(worktree), "reset", "--hard", ready_head],
            check=True, stdout=subprocess.DEVNULL,
        )
        calls.clear()
        wrong_lease = False
        transition_receipt_overrides = {}
        transition_response_overrides = {}
        transition_state = "Planning"
        transition_consumed = False
        commit_transition = True
        persist_transition = True
        terminal_overrides = {
            "kit_provenance_scope": "production-certified",
        }
        completed_events = len([
            CONTROL.read(path)
            for path in controller.events.glob("*.json")
            if CONTROL.read(path)["event"]
            == "repository_test_planner_completed"
        ])
        with patch.dict(os.environ, authority, clear=False):
            wrong_scope = CONTROL.Controller(self.args)
        wrong_scope.json_call = admission
        wrong_scope.refresh_dependency_tracking = lambda _claim: True
        wrong_scope.run_role = lambda *args: run_planner(wrong_scope, *args)
        wrong_scope_result = wrong_scope.reconcile()
        self.assertEqual(wrong_scope_result["status"], "error")
        self.assertIn(
            "repository-test planner did not complete with authenticated evidence",
            wrong_scope_result["results"][0]["error"],
        )
        self.assertEqual(
            len([
                CONTROL.read(path)
                for path in wrong_scope.events.glob("*.json")
                if CONTROL.read(path)["event"]
                == "repository_test_planner_completed"
            ]),
            completed_events,
        )

        for field, value in (
            ("phase", "failed"),
            ("accounting_state", "launch_void"),
            ("go_issued", "0"),
            ("task_submitted", "0"),
            ("exit_status", "1"),
            ("role_exit", "role_exit_no_commit"),
            ("role", "builder"),
            ("adapter", "codex"),
            ("selection_reason", "policy"),
            ("kit_sha", "0" * 40),
            ("transition_receipt_sha256", "0" * 64),
            ("role_head_before", "0" * 40),
            ("output_sha256", "0" * 64),
        ):
            (controller.claims / "T-110.json").unlink(missing_ok=True)
            (self.state / "T-110.json").unlink(missing_ok=True)
            subprocess.run(
                ["git", "-C", str(worktree), "reset", "--hard", ready_head],
                check=True, stdout=subprocess.DEVNULL,
            )
            calls.clear()
            terminal_overrides = {field: value}
            with self.subTest(terminal_field=field), patch.dict(
                os.environ, authority, clear=False,
            ):
                invalid_terminal = CONTROL.Controller(self.args)
                invalid_terminal.json_call = admission
                invalid_terminal.refresh_dependency_tracking = lambda _claim: True
                invalid_terminal.run_role = (
                    lambda *args: run_planner(invalid_terminal, *args)
                )
                invalid_result = invalid_terminal.reconcile()
            self.assertEqual(invalid_result["status"], "error")
            self.assertIn(
                "repository-test planner did not complete with authenticated evidence",
                invalid_result["results"][0]["error"],
            )
            self.assertEqual(
                len([
                    CONTROL.read(path)
                    for path in invalid_terminal.events.glob("*.json")
                    if CONTROL.read(path)["event"]
                    == "repository_test_planner_completed"
                ]),
                completed_events,
            )

        for mode in (
            "unchanged-head", "non-descendant-head", "unconsumed-receipt",
            "claim-receipt", "claim-role", "claim-status",
        ):
            (controller.claims / "T-110.json").unlink(missing_ok=True)
            (self.state / "T-110.json").unlink(missing_ok=True)
            subprocess.run(
                ["git", "-C", str(worktree), "reset", "--hard", ready_head],
                check=True, stdout=subprocess.DEVNULL,
            )
            calls.clear()
            terminal_overrides = {}
            planner_commits = mode != "unchanged-head"
            planner_non_descendant = mode == "non-descendant-head"
            leave_transition_unconsumed = mode == "unconsumed-receipt"
            claim_residue = (
                mode.removeprefix("claim-")
                if mode.startswith("claim-") else ""
            )
            with self.subTest(planner_lifecycle=mode), patch.dict(
                os.environ, authority, clear=False,
            ):
                invalid_lifecycle = CONTROL.Controller(self.args)
                invalid_lifecycle.json_call = admission
                invalid_lifecycle.refresh_dependency_tracking = lambda _claim: True
                invalid_lifecycle.run_role = (
                    lambda *args: run_planner(invalid_lifecycle, *args)
                )
                invalid_result = invalid_lifecycle.reconcile()
            self.assertEqual(invalid_result["status"], "error")
            self.assertIn(
                "repository-test planner did not complete with authenticated evidence",
                invalid_result["results"][0]["error"],
            )
            self.assertEqual(
                len([
                    CONTROL.read(path)
                    for path in invalid_lifecycle.events.glob("*.json")
                    if CONTROL.read(path)["event"]
                    == "repository_test_planner_completed"
                ]),
                completed_events,
            )

        (controller.claims / "T-110.json").unlink(missing_ok=True)
        calls.clear()
        terminal_overrides = {}
        planner_commits = True
        planner_non_descendant = False
        leave_transition_unconsumed = False
        claim_residue = ""
        with patch.dict(os.environ, authority, clear=False):
            replay = CONTROL.Controller(self.args)
        replay.json_call = admission
        replay.refresh_dependency_tracking = lambda _claim: True
        replay_result = replay.reconcile()
        self.assertEqual(replay_result["status"], "error")
        self.assertNotIn(("state-machine", "--ticket"), [
            call[:2] for call in calls
        ])
        self.assertIn(
            "repository-test requires a fresh Ready ticket",
            replay_result["results"][0]["error"],
        )

        for name in (
            "FACTORY_ADAPTER_OVERRIDE",
            "FACTORY_TEST_MODE",
            "FACTORY_TRUSTED_TEST_HARNESS",
        ):
            invalid = dict(authority)
            invalid[name] = ""
            with self.subTest(name=name), patch.dict(
                os.environ, invalid, clear=True,
            ), self.assertRaisesRegex(
                CONTROL.ControllerError,
                "repository-test controller authority is invalid",
            ):
                CONTROL.Controller(self.args)

        production = dict(authority)
        production["FACTORY_KIT_TRUST_SCOPE"] = "production-certified"
        with patch.dict(os.environ, production, clear=True):
            controller = CONTROL.Controller(self.args)
        calls = []

        def production_admission(*args, **_kwargs):
            calls.append(args[:2])
            if args[:2] == ("dispatch-plan", "--shadow"):
                return {"action": "SHADOW", "ticket": "T-110"}
            if args[:2] == ("models", "plan"):
                return self.model_resolution_error()
            raise AssertionError("production admission skipped model readiness")

        controller.json_call = production_admission
        with self.assertRaises(CONTROL.ControllerError):
            controller.claim_new([])
        self.assertEqual(calls, [
            ("dispatch-plan", "--shadow"),
            ("models", "plan"),
        ])

    def test_repository_test_refuses_every_preexisting_execution_source(self) -> None:
        authority = {
            "FACTORY_ADAPTER_OVERRIDE": "mock",
            "FACTORY_KIT_TRUST_SCOPE": "repository-test",
            "FACTORY_TEST_MODE": "1",
            "FACTORY_TRUSTED_TEST_HARNESS": "1",
        }
        cases = (
            ("claim", [{"ticket": "T-1"}], set(), {}),
            ("active-run", [], {"T-1"}, {}),
            ("lease", [], set(), {"T-1": {"ticket": "T-1"}}),
        )
        for label, claims, active, leases in cases:
            with self.subTest(source=label), patch.dict(
                os.environ, authority, clear=False,
            ):
                controller = CONTROL.Controller(self.args)
                controller.load_claims = lambda: claims
                controller.active_run_tickets = lambda: active
                controller.dispatcher_lease_records = lambda: leases
                with self.assertRaisesRegex(
                    CONTROL.ControllerError,
                    "repository-test Planning canary requires empty execution state",
                ):
                    controller.reconcile()

    def test_model_resolution_evidence_rejects_secret_key_families(self) -> None:
        controller = CONTROL.Controller(self.args)
        for label, unsafe in (
            ("authorization", "Authorization: Bearer DO-NOT-LEAK-A"),
            ("dsn", "dsn=DO-NOT-LEAK-B"),
            ("connection", "connection:DO-NOT-LEAK-C"),
        ):
            with self.subTest(label=label):
                failure = self.model_resolution_error()
                failure["readiness"]["codex-gpt-5.6-sol"][
                    "adapter_version"
                ] = unsafe
                with self.assertRaisesRegex(
                    CONTROL.ControllerError,
                    "model resolution failure is malformed",
                ):
                    controller.model_resolution_failure(
                        failure, "model plan failed",
                    )

        failure = self.model_resolution_error()
        failure["readiness"]["codex-gpt-5.6-sol"]["reported_identity"] = (
            "Authorization: Bearer DO-NOT-LEAK-D"
        )
        controller.record_admission_failure(
            CONTROL.ControllerError(CONTROL.canonical({
                **failure, "ticket": "T-110",
            })),
            [],
        )
        raw = (self.state / "admission-incident.json").read_text(encoding="utf-8")
        self.assertNotIn("DO-NOT-LEAK", raw)
        incident = json.loads(raw)
        self.assertEqual(incident["reason_code"], "unsafe_state")
        self.assertEqual(incident["ticket"], "T-110")

    def test_temporary_model_profile_waits_before_real_claim(self) -> None:
        controller = CONTROL.Controller(self.args)
        calls = []

        def admission(*args, **_kwargs):
            calls.append(args)
            if args[:2] == ("dispatch-plan", "--shadow"):
                return {"action": "SHADOW", "ticket": "T-110"}
            if args[:2] == ("models", "plan"):
                return self.model_resolution_error(
                    "profile_temporarily_unavailable",
                )
            raise AssertionError("real claim must wait for model readiness")

        controller.json_call = admission
        self.assertEqual(controller.claim_new([]), [])
        self.assertEqual(
            [call[:2] for call in calls],
            [("dispatch-plan", "--shadow"), ("models", "plan")],
        )
        events = [CONTROL.read(path) for path in controller.events.glob("*.json")]
        wait = next(item for item in events if item["event"] == "model_admission_wait")
        self.assertEqual(wait["ticket"], "T-110")
        self.assertEqual(
            wait["reason_code"], "profile_temporarily_unavailable",
        )

    def test_reconcile_reports_permanent_model_admission_failure(self) -> None:
        controller = CONTROL.Controller(self.args)

        def admission(*args, **_kwargs):
            if args[:2] == ("dispatch-plan", "--shadow"):
                return {"action": "SHADOW", "ticket": "T-110"}
            if args[:2] == ("models", "plan"):
                return self.model_resolution_error()
            raise AssertionError("permanent admission failure reached a claim")

        controller.json_call = admission
        result = controller.reconcile()

        self.assertEqual(result["active"], 0)
        self.assertEqual(result["results"], [{
            "profile_id": "cursor-opus-v1",
            "readiness": self.model_resolution_error()["readiness"],
            "reason_code": "profile_resolution_failed",
            "status": "error",
            "ticket": "T-110",
        }])

    def test_reconcile_reports_temporary_model_admission_wait(self) -> None:
        controller = CONTROL.Controller(self.args)
        temporary = self.model_resolution_error(
            "profile_temporarily_unavailable",
        )

        def admission(*args, **_kwargs):
            if args[:2] == ("dispatch-plan", "--shadow"):
                return {"action": "SHADOW", "ticket": "T-110"}
            if args[:2] == ("models", "plan"):
                return temporary
            raise AssertionError("temporary admission wait reached a claim")

        controller.json_call = admission
        result = controller.reconcile()

        self.assertEqual(result["active"], 0)
        self.assertEqual(result["results"], [{
            "profile_id": "cursor-opus-v1",
            "readiness": temporary["readiness"],
            "reason_code": "profile_temporarily_unavailable",
            "status": "waiting",
            "ticket": "T-110",
        }])

    def test_claim_race_wait_after_healthy_shadow_is_not_malformed(self) -> None:
        controller = CONTROL.Controller(self.args)
        calls = []

        def admission(*args, **_kwargs):
            calls.append(args)
            if args[:2] == ("dispatch-plan", "--shadow"):
                return {"action": "SHADOW", "ticket": "T-110"}
            if args[:2] == ("models", "plan"):
                return self.healthy_model_plan()
            return {"action": "WAIT", "reason_code": "claim_race"}

        controller.json_call = admission
        self.assertEqual(controller.claim_new([]), [])
        self.assertEqual(
            [call[:2] for call in calls],
            [
                ("dispatch-plan", "--shadow"),
                ("models", "plan"),
                ("dispatch-plan", "--claim"),
            ],
        )

    def test_post_shadow_invalid_drift_retains_claim_with_typed_incident(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        cell.mkdir()
        calls = []
        real_claims = iter(({
            "action": "START",
            "branch": "ticket/T-110",
            "lease_id": "a" * 64,
            "priority": "normal",
            "ticket": "T-110",
            "worktree": str(cell),
        }, {"action": "WAIT"}))

        def admission(*args, **_kwargs):
            calls.append(args)
            if args[:2] == ("dispatch-plan", "--shadow"):
                return {"action": "SHADOW", "ticket": "T-110"}
            if args[:2] == ("models", "plan"):
                return self.healthy_model_plan()
            if args[:2] == ("dispatch-plan", "--claim"):
                return next(real_claims)
            if args[:2] == ("models", "pin-batch"):
                return self.model_resolution_error(operation="pin")
            raise AssertionError(f"unexpected command: {args}")

        controller.json_call = admission
        claims = controller.claim_new([])
        with self.assertRaises(CONTROL.ControllerError) as raised:
            controller.pin_routes(claims)
        self.assertIn("model pin resolution failed", str(raised.exception))

        retained = controller.load_claims()
        self.assertEqual([claim["ticket"] for claim in retained], ["T-110"])
        self.assertEqual(retained[0]["lease"], "a" * 64)
        self.assertTrue(cell.is_dir())
        incident = CONTROL.read(self.state / "admission-incident.json")
        self.assertEqual(incident["ticket"], "T-110")
        self.assertEqual(incident["reason_code"], "profile_resolution_failed")
        self.assertEqual(
            incident["readiness"]["codex-gpt-5.6-sol"]["reason"],
            "version_mismatch",
        )
        self.assertEqual(
            [call[:2] for call in calls].count(("models", "plan")), 1,
        )
        self.assertFalse(any(call[0] == "release" for call in calls))

    def test_claims_four_cells_and_recovers_terminal_receipt(self) -> None:
        controller = CONTROL.Controller(self.args)
        values = [
            {
                "action": "START",
                "branch": f"ticket/T-{110 + number}",
                "lease_id": f"{number + 1:064x}",
                "priority": "normal",
                "ticket": f"T-{110 + number}",
                "worktree": str(self.root / f"cell-{number + 1}"),
            }
            for number in range(4)
        ]
        for value in values:
            Path(value["worktree"]).mkdir()
        values.append({"action": "WAIT"})
        calls = []

        def admission(*args, **_kwargs):
            calls.append(args)
            if args[:2] == ("dispatch-plan", "--shadow"):
                return {"action": "SHADOW", "ticket": "T-110"}
            if args[:2] == ("models", "plan"):
                return self.healthy_model_plan()
            return values.pop(0)

        controller.json_call = admission
        claims = controller.claim_new([])
        self.assertEqual(len(claims), 4)
        self.assertEqual(
            [call[:2] for call in calls].count(("models", "plan")), 1,
        )
        self.assertEqual(
            {item["ticket"] for item in controller.load_claims()},
            {"T-110", "T-111", "T-112", "T-113"},
        )

        claim = claims[0]
        claim.update(
            receipt="a" * 64,
            role="planner",
            status="running",
        )
        controller.save_claim(claim)
        (self.product / "factory/runs/run-1.meta").write_text(
            "run_id=run-1\n"
            "ticket=T-110\n"
            "role=planner\n"
            "accounting_state=completed\n"
            "exit_status=0\n"
            "role_exit=ok\n"
            f"transition_receipt_sha256={'a' * 64}\n",
            encoding="utf-8",
        )
        exports = []
        controller.passport = lambda item, state: exports.append((item["ticket"], state))
        self.assertTrue(controller.finish_pending_run(claim))
        self.assertEqual(exports, [("T-110", "none")])
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["status"], "claimed")

    def test_reviewer_passport_migrates_before_qualification_cell_parks(self) -> None:
        controller = CONTROL.Controller(self.args)
        receipt = "b" * 64
        claim = {
            "branch": "ticket/T-110",
            "lease": "c" * 64,
            "publication_lease": "",
            "receipt": receipt,
            "role": "reviewer",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "running",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        Path(claim["worktree"]).mkdir()
        (self.product / "factory/runs/reviewer.meta").write_text(
            "run_id=reviewer\n"
            "ticket=T-110\n"
            "role=reviewer\n"
            "accounting_state=completed\n"
            "exit_status=0\n"
            "role_exit=ok\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )
        calls = []
        controller.passport = lambda *_args: calls.append("export")
        controller.migrate_passport = lambda *_args: calls.append("migrate")
        controller.relocate_qualification_cell = lambda *_args: calls.append("park")
        controller.json_call = lambda *args, **_kwargs: calls.append(args[0]) or {}
        controller.event = lambda *_args, **_kwargs: None

        self.assertTrue(controller.finish_pending_run(claim))
        self.assertLess(calls.index("migrate"), calls.index("park"))

    def test_narrator_preview_wait_is_head_bound_and_bounded(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = {
            "branch": "ticket/T-110",
            "lease": "c" * 64,
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        first = {
            "head": "d" * 40,
            "preview_identity": {
                "expected": "d" * 40,
                "observed": [{"service": "api", "sha": "e" * 40}],
                "status": "wait",
            },
        }
        with patch.object(CONTROL.time, "time", return_value=1000):
            self.assertTrue(controller.wait_for_preview_identity(claim, first))
        self.assertEqual(claim["preview_wait_started_epoch"], 1000)

        preflight_claim = {
            key: item for key, item in claim.items()
            if key not in {"preview_wait_head", "preview_wait_started_epoch"}
        }
        paired = {
            "head": "a" * 40,
            "preview_identity": {
                "expected": "a" * 40,
                "observed": [{"service": "api", "sha": "a" * 40}],
                "status": "pass",
            },
            "preview_preflight": {
                "evidence": {"observed_api": "production"},
                "head": "a" * 40,
                "reason": "production_origin",
                "status": "wait",
            },
        }
        with patch.object(CONTROL.time, "time", return_value=1050):
            self.assertTrue(
                controller.wait_for_preview_identity(preflight_claim, paired)
            )
        self.assertEqual(preflight_claim["preview_wait_started_epoch"], 1050)

        changed = copy.deepcopy(first)
        changed["head"] = changed["preview_identity"]["expected"] = "f" * 40
        with patch.object(CONTROL.time, "time", return_value=1100):
            self.assertTrue(controller.wait_for_preview_identity(claim, changed))
        self.assertEqual(claim["preview_wait_started_epoch"], 1100)
        claim["preview_wait_started_epoch"] = 0
        controller.withdraw_publication = lambda *_args: None
        controller.release_ticket_lease = lambda item: item.update(lease_released=True)
        with patch.object(CONTROL.time, "time", return_value=1100):
            self.assertFalse(controller.wait_for_preview_identity(claim, changed))
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(claim["blocked_reason"], "preview-identity-timeout")

    def test_preview_timeout_retry_is_exact_and_provider_free(self) -> None:
        controller, claim, cell, passport, transition = (
            self.semantic_wait_fixture("preview-timeout-retry", "T-221")
        )
        transition.pop("loop", None)
        transition.update(role="narrator", stage="RUN narrator")
        transition["receipt_sha256"] = hashlib.sha256(
            CONTROL.canonical_document({
                key: value for key, value in transition.items()
                if key not in {"consumed", "receipt_sha256"}
            })
        ).hexdigest()
        CONTROL.write(self.state / "T-221.json", transition)
        claim.update(
            blocked_reason="preview-identity-timeout",
            lease_released=True,
            preview_wait_head=passport["head_sha"],
            preview_wait_started_epoch=0,
            status="blocked",
        )
        controller.save_claim(claim)
        controller.remote_passport_valid = lambda *_args: True
        controller.event = lambda name, *_args, **_kwargs: self.assertEqual(
            name, "preview_identity_timeout_retry_authorized",
        )

        before = controller.claim_path("T-221").read_bytes()
        with (
            patch.object(CONTROL.time, "time", return_value=800),
            self.assertRaisesRegex(
                CONTROL.ControllerError, "authority is unavailable",
            ),
        ):
            controller.retry_preview_timeout("T-221", "operator")
        self.assertEqual(controller.claim_path("T-221").read_bytes(), before)

        dirty = cell / "untracked"
        dirty.write_text("dirty\n", encoding="utf-8")
        with (
            patch.object(CONTROL.time, "time", return_value=1000),
            self.assertRaisesRegex(
                CONTROL.ControllerError, "authority is unavailable",
            ),
        ):
            controller.retry_preview_timeout("T-221", "operator")
        dirty.unlink()
        with patch.object(CONTROL.time, "time", return_value=1000):
            result = controller.retry_preview_timeout("T-221", "operator")
        self.assertEqual(result, {
            "expected": passport["head_sha"], "schema": CONTROL.SCHEMA,
            "status": "retry-authorized", "ticket": "T-221",
        })
        retried = CONTROL.read(controller.claim_path("T-221"))
        self.assertEqual(retried["status"], "claimed")
        self.assertNotIn("blocked_reason", retried)
        self.assertEqual(retried["preview_wait_started_epoch"], 1000)
        self.assertTrue(retried["lease_released"])

        parked_cell = self.root / "parked" / "T-221"
        parked_cell.parent.mkdir()
        cell.rename(parked_cell)
        claim.update(
            blocked_reason="preview-identity-timeout", lease="", parked=True,
            preview_wait_started_epoch=0, status="blocked",
            worktree=str(parked_cell),
        )
        claim.pop("lease_released", None)
        controller.worktrees_by_branch = lambda: {
            "refs/heads/ticket/T-221": [str(parked_cell)],
        }
        controller.save_claim(claim)
        with patch.object(CONTROL.time, "time", return_value=1000):
            result = controller.retry_preview_timeout("T-221", "operator")
        self.assertEqual(result["status"], "retry-authorized")
        parked = CONTROL.read(controller.claim_path("T-221"))
        self.assertEqual(parked["lease"], "")
        self.assertTrue(parked["parked"])
        self.assertNotIn("lease_released", parked)

    def test_exact_terminal_request_recovers_only_terminal_controller_error(self) -> None:
        controller = CONTROL.Controller(self.args)
        worktree = self.root / "parked/T-110"
        worktree.mkdir(parents=True)
        passport = {
            "passport_sha256": "d" * 64,
            "publication_state": "merged",
            "ticket": "T-110",
        }
        passports = self.state / "passports"
        passports.mkdir()
        CONTROL.write(passports / "T-110.json", passport)
        CONTROL.write(controller.terminal_request_path("T-110"), {
            "request_sha256": "e" * 64,
        })
        claim = {
            "blocked_reason": "controller-error",
            "branch": "ticket/T-110",
            "lease": "",
            "parked": True,
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(worktree),
        }
        controller.role_active = lambda _claim: False
        controller.remote_passport_valid = lambda _claim: True
        controller.terminal_request = lambda *_args, **_kwargs: {"action": "done"}
        controller.ensure_lease = lambda item, _label: item.update(lease="f" * 64)
        clean = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch.object(CONTROL.subprocess, "run", return_value=clean):
            controller.recover_terminal_requests([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertNotIn("blocked_reason", claim)
        self.assertEqual(claim["lease"], "f" * 64)

        unrelated = dict(claim, status="blocked", blocked_reason="worker-error")
        unrelated["lease"] = ""
        with patch.object(CONTROL.subprocess, "run", return_value=clean):
            controller.recover_terminal_requests([unrelated])
        self.assertEqual(unrelated["status"], "blocked")

    def test_ticket_merge_uses_the_approval_bound_pr_amid_branch_history(self) -> None:
        (self.product / "factory/PROJECT.env").write_text(
            "GH_REPO=example/product\nMAX_CONCURRENT_TICKETS=4\n",
            encoding="utf-8",
        )
        worktree = self.root / "parked/T-182"
        approval = worktree / "factory/attestations/T-182/approval.json"
        approval.parent.mkdir(parents=True)
        approval.write_text(json.dumps({
            "schema": "nysa.software-factory.ticket-approval/v1",
            "ticket": "T-182",
            "repository": "example/product",
            "branch": "ticket/T-182",
            "pr_number": 359,
        }), encoding="utf-8")
        claim = {
            "branch": "ticket/T-182", "ticket": "T-182",
            "worktree": str(worktree),
        }
        evidence = {
            "number": 359,
            "headRefName": "ticket/T-182",
            "baseRefName": "main",
            "headRefOid": "b" * 40,
            "mergeCommit": {"oid": "c" * 40},
            "state": "MERGED",
            "mergedAt": "2026-08-06T09:21:00Z",
        }
        calls = []

        def execute(arguments, **_kwargs):
            calls.append(arguments)
            return subprocess.CompletedProcess(
                arguments, 0, json.dumps(evidence), "",
            )

        controller = CONTROL.Controller(self.args)
        with patch.object(CONTROL.subprocess, "run", side_effect=execute):
            self.assertTrue(controller.ticket_merged(claim))

        self.assertEqual(calls[0][0:4], ["gh", "pr", "view", "359"])

        evidence["headRefName"] = "ticket/T-legacy"
        with (
            patch.object(CONTROL.subprocess, "run", side_effect=execute),
            self.assertRaisesRegex(
                CONTROL.ControllerError, "merged PR identity is malformed",
            ),
        ):
            controller.ticket_merged(claim)

    def test_terminal_request_allows_only_unrelated_protected_main_advance(self) -> None:
        controller = CONTROL.Controller(self.args)
        (self.product / "factory/PROJECT.env").write_text(
            "GH_REPO=example/product\n", encoding="utf-8",
        )
        passports = self.state / "passports"
        passports.mkdir()
        CONTROL.write(passports / "T-110.json", {
            "branch": "ticket/T-110",
            "factory_sha": "a" * 40,
            "passport_sha256": "d" * 64,
            "publication_state": "merged",
            "ticket": "T-110",
        })
        claim = {"branch": "ticket/T-110", "ticket": "T-110"}
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.parent.mkdir(parents=True)
        ticket.write_text("State: Done\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", "-b", "main", self.product], check=True)
        for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid")):
            subprocess.run(
                ["git", "-C", self.product, "config", key, value], check=True,
            )
        subprocess.run(["git", "-C", self.product, "add", "factory"], check=True)
        subprocess.run(
            ["git", "-C", self.product, "commit", "-qm", "terminal"], check=True,
        )
        terminal = subprocess.run(
            ["git", "-C", self.product, "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", remote], check=True)
        subprocess.run(
            ["git", "-C", self.product, "remote", "add", "origin", remote],
            check=True,
        )
        subprocess.run(
            ["git", "-C", self.product, "push", "-q", "origin", "main"], check=True,
        )
        controller.approval_pr_number = lambda _claim: 1
        controller.merged_pr_identity = lambda branch, number=None: {
            "head": "a" * 40,
            "merge_commit": terminal,
            "number": 2 if "closeout" in branch else 1,
        }
        request = controller.terminal_request(
            claim, "chore/t110-closeout", create=True,
        )
        self.assertEqual(
            request["schema"], "nysa.software-factory.terminal-request/v2",
        )

        (self.product / "unrelated.txt").write_text("safe\n", encoding="utf-8")
        subprocess.run(["git", "-C", self.product, "add", "unrelated.txt"], check=True)
        subprocess.run(
            ["git", "-C", self.product, "commit", "-qm", "unrelated advance"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", self.product, "push", "-q", "origin", "main"], check=True,
        )
        self.assertEqual(
            controller.terminal_request(claim, "chore/t110-closeout", create=False),
            request,
        )

        ticket.write_text("State: Done\nchanged\n", encoding="utf-8")
        subprocess.run(["git", "-C", self.product, "add", str(ticket)], check=True)
        subprocess.run(
            ["git", "-C", self.product, "commit", "-qm", "change ticket"], check=True,
        )
        subprocess.run(
            ["git", "-C", self.product, "push", "-q", "origin", "main"], check=True,
        )
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "terminal request identity changed",
        ):
            controller.terminal_request(
                claim, "chore/t110-closeout", create=False,
            )

    def test_launch_void_blocks_once_and_preserves_role_receipt(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {"generation": 1, "tickets": ["T-110"]}
        receipt = "b" * 64
        claim = {
            "branch": "ticket/T-110",
            "lease": "c" * 64,
            "publication_lease": "",
            "receipt": receipt,
            "role": "test-author",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "running",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        Path(claim["worktree"]).mkdir()
        controller.save_claim(claim)
        for number in (1, 2):
            (self.product / f"factory/runs/void-{number}.meta").write_text(
                f"run_id=void-{number}\n"
                "phase=abandoned\n"
                "ticket=T-110\n"
                "role=test-author\n"
                "accounting_state=launch_void\n"
                "go_issued=0\n"
                "task_submitted=0\n"
                "effective_cost=0\n"
                "cost_basis=launch_void\n"
                "exit_status=6\n"
                "role_exit=\n"
                f"kit_sha={'a' * 40}\n"
                f"role_head_before={'d' * 40}\n"
                "terminal_reason_code=cursor_credential_unsafe\n"
                f"terminal_at=2026-07-30T15:0{number}:00Z\n"
                f"transition_receipt_sha256={receipt}\n",
                encoding="utf-8",
            )
        calls = []
        controller.json_call = lambda *args, **_kwargs: calls.append(args) or {}
        controller.passport = lambda *_args: self.fail(
            "pre-GO attempts must not replace successful-role passport evidence"
        )

        self.assertFalse(controller.finish_pending_run(claim))
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(claim["receipt"], receipt)
        self.assertEqual(claim["role"], "test-author")
        self.assertTrue(claim["lease_released"])
        self.assertEqual(calls, [
            ("release", "--ticket", "T-110", "--lease", "c" * 64),
        ])
        events = [
            CONTROL.read(path) for path in sorted(self.state.glob("events/*.json"))
        ]
        terminal = next(
            item for item in events if item["event"] == "attempt_terminal"
        )
        self.assertEqual(terminal["run_id"], "void-2")
        self.assertEqual(terminal["duplicate_launch_void_count"], 2)
        blocked = next(
            item for item in events if item["event"] == "pre_go_failure_blocked"
        )
        self.assertEqual(blocked["reason"], "cursor_credential_unsafe")
        self.assertTrue(controller.qualification_cohort_error.is_set())

    def test_prior_release_launch_void_retries_stage_once(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {"generation": 1, "tickets": ["T-110"]}
        receipt = "b" * 64
        claim = {
            "branch": "ticket/T-110",
            "lease": "c" * 64,
            "publication_lease": "",
            "receipt": receipt,
            "role": "test-author",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "running",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        Path(claim["worktree"]).mkdir()
        controller.save_claim(claim)
        (self.product / "factory/runs/prior-void.meta").write_text(
            "run_id=prior-void\n"
            "phase=abandoned\n"
            "ticket=T-110\n"
            "role=test-author\n"
            "accounting_state=launch_void\n"
            "go_issued=0\n"
            "task_submitted=0\n"
            "effective_cost=0\n"
            "cost_basis=launch_void\n"
            "exit_status=6\n"
            "role_exit=\n"
            f"kit_sha={'e' * 40}\n"
            f"role_head_before={'d' * 40}\n"
            "terminal_reason_code=cursor_credential_unsafe\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )

        self.assertTrue(controller.finish_pending_run(claim))
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["role"], "")
        self.assertTrue(controller.finish_pending_run(claim))
        events = [
            CONTROL.read(path) for path in sorted(self.state.glob("events/*.json"))
        ]
        self.assertEqual(
            sum(
                item["event"] == "pre_go_failure_recovered_by_release_upgrade"
                for item in events
            ),
            1,
        )
        self.assertFalse(controller.qualification_cohort_error.is_set())

    def test_attempt_progress_is_content_free_and_monotonic(self) -> None:
        controller = CONTROL.Controller(self.args)
        receipt = "b" * 64
        claim = {
            "receipt": receipt,
            "role": "builder",
            "ticket": "T-110",
        }
        (self.product / "factory/runs/live.meta").write_text(
            "run_id=live\n"
            "ticket=T-110\n"
            "role=builder\n"
            "adapter=cursor-anthropic\n"
            "route_id=cursor-claude\n"
            "provider_attempt_id=live-cli\n"
            "accounting_state=reserved\n"
            "go_issued=1\n"
            "task_submitted=1\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )
        progress = self.product / "factory/runs/live.progress.jsonl"
        records = [
            {
                "event_sha256": "c" * 64,
                "observed_monotonic_ns": 100,
                "sequence": 1,
                "subtype": "",
                "type": "assistant",
            },
            {
                "event_sha256": "d" * 64,
                "observed_monotonic_ns": 200,
                "sequence": 2,
                "subtype": "completed",
                "type": "tool_call",
            },
        ]
        progress.write_text(json.dumps(records[0]) + "\n", encoding="utf-8")
        progress.chmod(0o600)

        controller.observe_attempt(claim)
        controller.observe_attempt(claim)
        with progress.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(records[1]) + "\n")
        controller.observe_attempt(claim)
        (self.product / "factory/runs/duplicate-live.meta").write_text(
            (self.product / "factory/runs/live.meta").read_text(
                encoding="utf-8"
            ).replace("run_id=live\n", "run_id=duplicate-live\n"),
            encoding="utf-8",
        )
        controller.observe_attempt_safely(claim)

        events = [
            CONTROL.read(path) for path in sorted(self.state.glob("events/*.json"))
        ]
        self.assertEqual(
            [item["event"] for item in events].count("attempt_bound"), 1
        )
        progress_events = [
            item for item in events if item["event"] == "attempt_progress"
        ]
        self.assertEqual(
            [item["progress_events"] for item in progress_events], [1, 2]
        )
        self.assertEqual(progress_events[-1]["latest_type"], "tool_call")
        self.assertNotIn("output", progress_events[-1])
        self.assertEqual(
            [item["event"] for item in events].count(
                "attempt_observation_invalid"
            ),
            1,
        )

    def test_qualification_forces_real_restart_before_four_ticket_run(self) -> None:
        tickets = [f"T-{number}" for number in range(110, 114)]
        (self.product / "factory/QUALIFICATION.json").write_text(
            json.dumps({
                "budget_usd": "100.000000",
                "capacity": 4,
                "contract_version": "1.8.0",
                "factory_sha": "a" * 40,
                "generation": 1,
                "per_run_budget_usd": "2.000000",
                "per_ticket_budget_usd": "25.000000",
                "schema": CONTROL.QUALIFICATION_SCHEMA,
                "target_done": 4,
                "tickets": tickets,
            }),
            encoding="utf-8",
        )
        stale = {
            "factory_sha": "b" * 40,
            "schema": CONTROL.EVENT_SCHEMA,
            "tickets": tickets,
        }
        for name in (
            "qualification-restart-boundary", "qualification-recovered",
        ):
            (self.state / f"{name}.json").write_text(
                json.dumps(stale), encoding="utf-8",
            )
        first = CONTROL.Controller(self.args)
        first.qualification_admission_preflight = lambda _claims: None
        values = []
        for number, ticket in enumerate(tickets, 1):
            cell = self.root / f"cell-{number}"
            cell.mkdir()
            values.append({
                "action": "START",
                "branch": f"ticket/{ticket}",
                "lease_id": f"{number:064x}",
                "priority": "normal",
                "ticket": ticket,
                "worktree": str(cell),
            })
        def json_call(*parts, **_kwargs):
            if parts[:2] == ("models", "qualification-readiness"):
                return {
                    "readiness_sha256": "f" * 64,
                    "schema": (
                        "nysa.software-factory.qualification-"
                        "fallback-readiness/v1"
                    ),
                    "status": "ready",
                }
            return values.pop(0)

        first.json_call = json_call
        self.assertEqual(first.reconcile()["status"], "restart_required")
        current_boundary = self.state / (
            f"qualification-restart-boundary-{'a' * 40}.json"
        )
        self.assertEqual(
            json.loads(current_boundary.read_text(encoding="utf-8")),
            {
                "factory_sha": "a" * 40,
                "schema": CONTROL.EVENT_SCHEMA,
                "tickets": tickets,
            },
        )

        second = CONTROL.Controller(self.args)
        second.qualification_admission_preflight = lambda _claims: None
        second.pin_routes = lambda _claims: []
        second.reconcile_ticket = lambda claim: {
            "status": "active", "ticket": claim["ticket"],
        }
        result = second.reconcile()
        self.assertEqual(result["active"], 4)
        events = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (self.state / "events").glob("*.json")
        ]
        self.assertIn("restart_boundary", {item["event"] for item in events})
        self.assertIn("controller_recovered", {item["event"] for item in events})
        self.assertTrue(
            (self.state / f"qualification-recovered-{'a' * 40}.json").is_file()
        )

    def test_qualification_restart_preserves_blocked_target_claims(self) -> None:
        tickets = [f"T-{number}" for number in range(110, 113)]
        (self.product / "factory/PROJECT.env").write_text(
            "MAX_CONCURRENT_TICKETS=3\n", encoding="utf-8"
        )
        (self.product / "factory/QUALIFICATION.json").write_text(
            json.dumps({
                "budget_usd": "300.000000",
                "capacity": 3,
                "contract_version": "1.8.0",
                "factory_sha": "a" * 40,
                "generation": 2,
                "mode": "successor",
                "per_run_budget_usd": "10.000000",
                "per_ticket_budget_usd": "100.000000",
                "schema": CONTROL.QUALIFICATION_SCHEMA,
                "source_factory_sha": "b" * 40,
                "target_done": 3,
                "tickets": tickets,
            }),
            encoding="utf-8",
        )
        first = CONTROL.Controller(self.args)
        first.qualification_admission_preflight = lambda _claims: None
        for number, ticket in enumerate(tickets, 1):
            cell = self.root / f"cell-{number}"
            cell.mkdir()
            first.save_claim({
                "blocked_reason": "preflight" if number > 1 else "",
                "branch": f"ticket/{ticket}",
                "lease": f"{number:064x}",
                "lease_released": number > 1,
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "blocked" if number > 1 else "claimed",
                "ticket": ticket,
                "worktree": str(cell),
            })
        first.recover_missing_passport_claims = lambda _claims: None
        first.recover_upgraded_claims = lambda _claims: None
        first.recover_terminal_exports = lambda _claims: None
        first.recover_repaired_failures = lambda _claims: None
        first.claim_new = lambda claims: claims

        result = first.reconcile()
        self.assertEqual(result["status"], "restart_required")
        self.assertEqual(result["active"], 1)

        second = CONTROL.Controller(self.args)
        second.qualification_admission_preflight = lambda _claims: None
        second.recover_missing_passport_claims = lambda _claims: None
        second.recover_upgraded_claims = lambda _claims: None
        second.recover_terminal_exports = lambda _claims: None
        second.recover_repaired_failures = lambda _claims: None
        second.claim_new = lambda claims, *_args: claims
        maintained = []
        second.maintain_successor_leases = lambda claims: maintained.append(
            sorted(claim["ticket"] for claim in claims)
        )
        second.pin_routes = lambda claims: [
            {"status": "waiting", "ticket": claim["ticket"]}
            for claim in claims
        ]
        second.reconcile()

        self.assertIn(tickets, maintained)
        self.assertTrue(
            (self.state / f"qualification-recovered-{'a' * 40}.json").is_file()
        )
        claims = {item["ticket"]: item for item in second.load_claims()}
        self.assertEqual(claims["T-111"]["status"], "blocked")
        self.assertEqual(claims["T-112"]["status"], "blocked")
        self.assertFalse((self.state / "passports").exists())
        self.assertEqual(list((self.product / "factory/runs").iterdir()), [])

    def test_qualification_restart_counts_protected_done_ticket(self) -> None:
        tickets = [f"T-{number}" for number in range(110, 113)]
        source_factory = "b" * 40
        (self.product / "factory/PROJECT.env").write_text(
            "MAX_CONCURRENT_TICKETS=3\n", encoding="utf-8"
        )
        (self.product / "factory/QUALIFICATION.json").write_text(
            json.dumps({
                "budget_usd": "300.000000",
                "capacity": 3,
                "contract_version": "1.8.0",
                "factory_sha": "a" * 40,
                "generation": 1,
                "mode": "successor",
                "per_run_budget_usd": "10.000000",
                "per_ticket_budget_usd": "100.000000",
                "schema": CONTROL.QUALIFICATION_SCHEMA,
                "source_factory_sha": source_factory,
                "target_done": 3,
                "tickets": tickets,
            }),
            encoding="utf-8",
        )
        (self.product / "factory/tickets").mkdir()
        (self.product / "factory/tickets/T-110.md").write_text(
            "State: Done\n", encoding="utf-8"
        )
        first = CONTROL.Controller(self.args)
        first.qualification_admission_preflight = lambda _claims: None
        passport_path = self.state / "passports/T-110.json"
        passport_path.parent.mkdir(mode=0o700)
        source_passport = "c" * 64
        done_kit = "c" * 40
        passport_factory = "9" * 40
        pr_head = "d" * 40
        post_merge_head = "7" * 40
        protected_base = "4" * 40
        route = "5" * 64
        merge_commit = "e" * 40
        CONTROL.write(passport_path, {
            "branch": "ticket/T-110",
            "charge_records": [],
            "completed_role_evidence": [],
            "current_state": "Approved",
            "factory_release_history": [{
                "contract_version": "1.8.0", "factory_sha": done_kit,
            }, {
                "contract_version": "1.8.0", "factory_sha": source_factory,
            }, {
                "contract_version": "1.8.0", "factory_sha": passport_factory,
            }],
            "factory_sha": passport_factory,
            "head_sha": post_merge_head,
            "migration_history": [{
                "from_factory_sha": source_factory,
                "from_head_sha": pr_head,
                "from_passport_file_sha256": "2" * 64,
                "from_passport_sha256": "1" * 64,
                "from_protected_base_sha": "3" * 40,
                "from_route_plan_sha256": "6" * 64,
                "schema": "nysa.software-factory.ticket-passport-migration/v2",
                "to_factory_sha": passport_factory,
                "to_head_sha": post_merge_head,
                "to_protected_base_sha": protected_base,
                "to_route_plan_sha256": route,
            }],
            "parent_digest": "1" * 64,
            "parent_file_sha256": "2" * 64,
            "passport_sha256": source_passport,
            "protected_base_sha": protected_base,
            "publication_state": "merged",
            "route_plan_sha256": route,
            "ticket": "T-110",
        })
        done_path = self.product / "factory/attestations/T-110/done.json"
        done_path.parent.mkdir(parents=True)
        done_path.write_text(json.dumps({
            "approved_pr_head": pr_head,
            "kit_sha": done_kit,
            "merge_commit": merge_commit,
            "pr_number": 94,
            "schema": "nysa.software-factory.ticket-done/v1",
            "ticket": "T-110",
        }), encoding="utf-8")
        old_completion = {
            "event": "ticket_complete",
            "factory_sha": source_factory,
            "observed_at_epoch_ns": 1,
            "schema": CONTROL.EVENT_SCHEMA,
            "ticket": "T-110",
        }
        old_completion["event_sha256"] = hashlib.sha256(
            CONTROL.canonical(old_completion).encode()
        ).hexdigest()
        CONTROL.write(first.events / "old-completion.json", old_completion)
        first.worktrees_by_branch = lambda: {
            "refs/heads/ticket/T-110": [str(self.root / "terminal-cell")],
        }
        migrations = []

        def passport_call(*args, **_kwargs):
            passport = CONTROL.read(passport_path)
            if args[:2] == ("passport", "validate"):
                return {"passport": passport["passport_sha256"], "status": "ok"}
            if args[:2] == ("passport", "migrate"):
                migrations.append("T-110")
                self.assertEqual(
                    args[args.index("--publication-state") + 1], "preserve"
                )
                candidate_passport = "f" * 64
                passport.update({
                    "factory_sha": "a" * 40,
                    "parent_digest": source_passport,
                    "parent_file_sha256": "8" * 64,
                    "passport_sha256": candidate_passport,
                })
                passport["factory_release_history"].append({
                    "contract_version": "1.8.0", "factory_sha": "a" * 40,
                })
                passport["migration_history"].append({
                    "from_factory_sha": passport_factory,
                    "from_head_sha": post_merge_head,
                    "from_passport_file_sha256": "8" * 64,
                    "from_passport_sha256": source_passport,
                    "from_protected_base_sha": protected_base,
                    "from_route_plan_sha256": route,
                    "schema": "nysa.software-factory.ticket-passport-migration/v2",
                    "to_factory_sha": "a" * 40,
                    "to_head_sha": post_merge_head,
                    "to_protected_base_sha": protected_base,
                    "to_route_plan_sha256": route,
                })
                CONTROL.write(passport_path, passport)
                return {"passport": candidate_passport, "status": "ok"}
            raise AssertionError(args)

        first.json_call = passport_call
        disconnected = CONTROL.read(passport_path)
        disconnected["migration_history"][0]["from_factory_sha"] = "8" * 40
        CONTROL.write(passport_path, disconnected)
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "unknown release"
        ):
            first.record_qualification_done_targets()
        self.assertEqual(migrations, [])
        self.assertFalse(any(
            CONTROL.read(path).get("factory_sha") == "a" * 40
            for path in self.state.glob("events/*.json")
        ))
        disconnected["migration_history"][0][
            "from_factory_sha"
        ] = source_factory
        CONTROL.write(passport_path, disconnected)
        disconnected_head = copy.deepcopy(disconnected)
        disconnected_head["migration_history"][0]["from_head_sha"] = "8" * 40
        CONTROL.write(passport_path, disconnected_head)
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "source terminal is invalid"
        ):
            first.record_qualification_done_targets()
        self.assertEqual(migrations, [])
        substituted = copy.deepcopy(disconnected)
        substituted["migration_history"][0]["from_passport_sha256"] = "0" * 64
        CONTROL.write(passport_path, substituted)
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "source terminal is invalid"
        ):
            first.record_qualification_done_targets()
        self.assertEqual(migrations, [])
        CONTROL.write(passport_path, disconnected)
        regressed = CONTROL.read(passport_path)
        regressed["factory_release_history"].insert(-1, {
            "contract_version": "1.8.0", "factory_sha": "a" * 40,
        })
        regressed["migration_history"] = [{
            "from_factory_sha": source_factory,
            "schema": "nysa.software-factory.ticket-passport-migration/v2",
            "to_factory_sha": "a" * 40,
        }, {
            "from_factory_sha": "a" * 40,
            "schema": "nysa.software-factory.ticket-passport-migration/v2",
            "to_factory_sha": passport_factory,
        }]
        CONTROL.write(passport_path, regressed)
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "source terminal is invalid"
        ):
            first.record_qualification_done_targets()
        self.assertEqual(migrations, [])
        CONTROL.write(passport_path, disconnected)
        nonterminal = CONTROL.read(passport_path)
        nonterminal["publication_state"] = "validating"
        CONTROL.write(passport_path, nonterminal)
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "source terminal is invalid"
        ):
            first.record_qualification_done_targets()
        self.assertEqual(migrations, [])
        self.assertFalse(
            (
                self.state
                / f"qualification-terminal-adoption-{'a' * 40}-T-110.json"
            ).exists()
        )
        self.assertFalse(any(
            CONTROL.read(path).get("factory_sha") == "a" * 40
            for path in self.state.glob("events/*.json")
        ))
        nonterminal["publication_state"] = "merged"
        CONTROL.write(passport_path, nonterminal)
        for number, ticket in enumerate(tickets[1:], 1):
            cell = self.root / f"cell-{number}"
            cell.mkdir()
            first.save_claim({
                "branch": f"ticket/{ticket}",
                "lease": f"{number:064x}",
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "claimed",
                "ticket": ticket,
                "worktree": str(cell),
            })
        first.recover_missing_passport_claims = lambda _claims: None
        first.recover_upgraded_claims = lambda _claims: None
        first.recover_terminal_exports = lambda _claims: None
        first.recover_repaired_failures = lambda _claims: None
        first.claim_new = lambda claims: claims
        result = first.reconcile()
        self.assertEqual(result["status"], "restart_required")
        self.assertEqual(result["active"], 2)
        events = [
            CONTROL.read(path) for path in sorted(self.state.glob("events/*.json"))
        ]
        self.assertEqual(
            [
                item["ticket"] for item in events
                if item["event"] == "ticket_complete"
                and item["factory_sha"] == "a" * 40
            ],
            ["T-110"],
        )
        self.assertEqual(migrations, ["T-110"])
        self.assertEqual(CONTROL.read(passport_path)["factory_sha"], "a" * 40)

        second = CONTROL.Controller(self.args)
        second.qualification_admission_preflight = lambda _claims: None
        second.recover_missing_passport_claims = lambda _claims: None
        second.recover_upgraded_claims = lambda _claims: None
        second.recover_terminal_exports = lambda _claims: None
        second.recover_repaired_failures = lambda _claims: None
        second.claim_new = lambda claims: claims
        second.maintain_successor_leases = lambda _claims: None
        second.pin_routes = lambda _claims: []
        second.reconcile_ticket = lambda claim: {
            "status": "active", "ticket": claim["ticket"],
        }
        self.assertEqual(second.reconcile()["active"], 2)
        self.assertEqual(second.reconcile()["active"], 2)
        events = [
            CONTROL.read(path) for path in sorted(self.state.glob("events/*.json"))
        ]
        self.assertIn(
            {"event": "controller_recovered", "tickets": tickets},
            [
                {"event": item["event"], "tickets": item.get("tickets")}
                for item in events
            ],
        )
        self.assertEqual(
            [
                item["ticket"] for item in events
                if item["event"] == "ticket_complete"
                and item["factory_sha"] == "a" * 40
            ],
            ["T-110"],
        )
        self.assertEqual(
            [
                item["ticket"] for item in events
                if item["event"] == "terminal_adopted"
                and item["factory_sha"] == "a" * 40
            ],
            ["T-110"],
        )
        self.assertTrue(
            (
                self.state
                / f"qualification-terminal-adoption-{'a' * 40}-T-110.json"
            ).is_file()
        )
        self.assertNotIn(
            "T-110",
            {
                item["ticket"] for item in events
                if item["event"] in {"attempt_started", "ticket_claimed"}
            },
        )

        (self.product / "factory/tickets/T-111.md").write_text(
            "State: Done\n", encoding="utf-8"
        )
        CONTROL.write(self.state / "passports/T-111.json", {
            "charge_records": [{"factory_sha": "a" * 40}],
            "completed_role_evidence": [],
            "factory_sha": "a" * 40,
            "migration_history": [],
        })
        adoption_calls = []
        original_adoption = second.adopt_qualification_terminal

        def track_adoption(ticket):
            adoption_calls.append(ticket)
            return original_adoption(ticket)

        second.adopt_qualification_terminal = track_adoption
        second.record_qualification_done_targets()
        self.assertEqual(adoption_calls, ["T-110"])
        events = [
            CONTROL.read(path) for path in sorted(self.state.glob("events/*.json"))
        ]
        self.assertEqual(
            [
                item["ticket"] for item in events
                if item["event"] == "ticket_complete"
                and item["factory_sha"] == "a" * 40
            ],
            ["T-110", "T-111"],
        )

    def test_three_ticket_qualification_parks_an_excluded_claim(self) -> None:
        tickets = [f"T-{number}" for number in range(110, 113)]
        (self.product / "factory/PROJECT.env").write_text(
            "MAX_CONCURRENT_TICKETS=3\n", encoding="utf-8"
        )
        (self.product / "factory/QUALIFICATION.json").write_text(
            json.dumps({
                "budget_usd": "100.000000",
                "capacity": 3,
                "contract_version": "1.8.0",
                "factory_sha": "a" * 40,
                "generation": 2,
                "per_run_budget_usd": "2.000000",
                "per_ticket_budget_usd": "25.000000",
                "schema": CONTROL.QUALIFICATION_SCHEMA,
                "target_done": 3,
                "tickets": tickets,
            }),
            encoding="utf-8",
        )
        controller = CONTROL.Controller(self.args)
        controller.qualification_admission_preflight = lambda _claims: None
        for number, ticket in enumerate([*tickets, "T-113"], 1):
            cell = self.root / f"cell-{number}"
            cell.mkdir()
            controller.save_claim({
                "branch": f"ticket/{ticket}",
                "lease": f"{number:064x}",
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "claimed",
                "ticket": ticket,
                "worktree": str(cell),
            })
        seen = []
        controller.recover_missing_passport_claims = lambda claims: seen.extend(
            claim["ticket"] for claim in claims
        )
        controller.recover_upgraded_claims = lambda _claims: None
        controller.recover_repaired_failures = lambda _claims: None
        controller.claim_new = lambda claims: claims
        withdrawn = []
        controller.withdraw_publication = lambda claim: withdrawn.append(
            claim["ticket"]
        )
        result = controller.reconcile()
        self.assertEqual(result["status"], "restart_required")
        self.assertEqual(result["active"], 3)
        self.assertEqual(seen, tickets)
        self.assertEqual(withdrawn, ["T-113"])
        self.assertTrue(controller.claim_path("T-113").exists())

    def test_three_ticket_successor_qualification_accepts_live_envelope(self) -> None:
        tickets = [f"T-{number}" for number in range(110, 113)]
        (self.product / "factory/PROJECT.env").write_text(
            "MAX_CONCURRENT_TICKETS=3\n", encoding="utf-8"
        )
        (self.product / "factory/QUALIFICATION.json").write_text(
            json.dumps({
                "budget_usd": "300.000000",
                "capacity": 3,
                "contract_version": "1.8.0",
                "factory_sha": "a" * 40,
                "generation": 1,
                "mode": "successor",
                "per_run_budget_usd": "10.000000",
                "per_ticket_budget_usd": "100.000000",
                "schema": CONTROL.QUALIFICATION_SCHEMA,
                "source_factory_sha": "b" * 40,
                "target_done": 3,
                "tickets": tickets,
            }),
            encoding="utf-8",
        )
        controller = CONTROL.Controller(self.args)
        self.assertEqual(controller.qualification["mode"], "successor")
        self.assertEqual(controller.qualification["tickets"], tickets)

    def test_three_ticket_restart_includes_prior_release_launch_void(self) -> None:
        tickets = [f"T-{number}" for number in range(110, 113)]
        old_factory = "b" * 40
        receipt = "c" * 64
        (self.product / "factory/PROJECT.env").write_text(
            "MAX_CONCURRENT_TICKETS=3\n", encoding="utf-8"
        )
        (self.product / "factory/QUALIFICATION.json").write_text(
            json.dumps({
                "budget_usd": "300.000000",
                "capacity": 3,
                "contract_version": "1.8.0",
                "factory_sha": self.release.name,
                "generation": 1,
                "mode": "successor",
                "per_run_budget_usd": "10.000000",
                "per_ticket_budget_usd": "100.000000",
                "schema": CONTROL.QUALIFICATION_SCHEMA,
                "source_factory_sha": old_factory,
                "target_done": 3,
                "tickets": tickets,
            }),
            encoding="utf-8",
        )
        controller = CONTROL.Controller(self.args)
        controller.qualification_admission_preflight = lambda _claims: None
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        for number, ticket in enumerate(tickets, 1):
            cell = self.root / "parked" / ticket
            self.initialize_parked_branch(cell, f"ticket/{ticket}")
            controller.save_claim({
                "branch": f"ticket/{ticket}",
                "lease": f"{number:064x}",
                "parked": True,
                "priority": "normal",
                "publication_lease": "",
                "receipt": receipt if number == 1 else "",
                "role": "narrator" if number == 1 else "",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "blocked" if number == 1 else "claimed",
                "ticket": ticket,
                "worktree": str(cell),
            })
            CONTROL.write(
                passports / f"{ticket}.json", {"factory_sha": old_factory},
            )
        (self.product / "factory/runs/prior-void.meta").write_text(
            "run_id=prior-void\n"
            "phase=abandoned\n"
            "ticket=T-110\n"
            "role=narrator\n"
            "accounting_state=launch_void\n"
            "go_issued=0\n"
            "task_submitted=0\n"
            "effective_cost=0\n"
            "cost_basis=launch_void\n"
            "exit_status=6\n"
            "role_exit=\n"
            f"kit_sha={old_factory}\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )

        def migrate(claim, _publication):
            CONTROL.write(
                passports / f"{claim['ticket']}.json",
                {"factory_sha": self.release.name},
            )

        controller.recover_missing_passport_claims = lambda _claims: None
        controller.ticket_release_current = lambda _claim: True
        controller.renew = lambda _claim: None
        controller.release_ticket_lease = lambda claim: (
            claim.update(lease_released=True), controller.save_claim(claim)
        )[-1]
        controller.migrate_passport = migrate
        controller.restore_contract_blocker = lambda _claim: False
        controller.claim_new = lambda claims: claims

        result = controller.reconcile()

        self.assertEqual(result["status"], "restart_required")
        self.assertEqual(result["active"], 3)
        recovered = CONTROL.read(controller.claim_path("T-110"))
        self.assertEqual(recovered["status"], "running")
        self.assertEqual(recovered["receipt"], receipt)

    def test_preflight_runs_once_before_planner_only(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        cell.mkdir()
        claim = {
            "lease": "a" * 64,
            "publication_lease": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        calls = []
        controller.json_call = lambda *args, **kwargs: (
            calls.append((args, kwargs)) or {"exit_code": 0, "status": "ok"}
        )
        controller.terminal_for_receipt = lambda *_args: {}
        controller.finish_pending_run = lambda _claim: True

        controller.run_role(claim, "planner", "b" * 64, [])
        controller.run_role(claim, "builder", "c" * 64, [])

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][0], "preflight")
        self.assertIn("planner", calls[0][0])

    def test_preflight_refusal_evidence_survives_block_redacted_and_bounded(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        cell.mkdir()
        claim = {
            "lease": "a" * 64,
            "publication_lease": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        output = (
            "PREFLIGHT FAIL: Fixture-Seams path is not a regular file\n"
            "FAIL: token=do-not-persist https://secret.example.invalid/path\n"
            + "ignored\n" * 20
        )
        controller.json_call = lambda *_args, **_kwargs: {
            "exit_code": 1, "output": output, "status": "error",
        }
        controller.block = lambda item, reason: item.update(
            status="blocked", blocked_reason=reason, lease_released=True,
        )
        controller.run_role(claim, "planner", "b" * 64, [])

        events = [CONTROL.read(path) for path in controller.events.glob("*.json")]
        refusal = next(item for item in events if item["event"] == "preflight_refused")
        self.assertEqual(refusal["preflight_reason_code"], "deterministic_refusal")
        self.assertEqual(len(refusal["preflight_failure_lines"]), 2)
        self.assertNotIn("do-not-persist", json.dumps(refusal))
        self.assertNotIn("secret.example.invalid", json.dumps(refusal))
        self.assertEqual(
            refusal["preflight_output_sha256"], hashlib.sha256(output.encode()).hexdigest(),
        )
        self.assertEqual(claim["blocked_reason"], "preflight")
        self.assertEqual(
            len(list(CONTROL.Controller(self.args).events.glob("*.json"))), 1,
        )

        malformed = dict(claim, status="claimed")
        controller.json_call = lambda *_args, **_kwargs: {
            "exit_code": 1, "output": ["not text"], "status": "error",
        }
        controller.run_role(malformed, "planner", "c" * 64, [])
        self.assertEqual(malformed["blocked_reason"], "preflight-evidence")

    def test_passport_preflight_correction_reissues_exact_planner_receipt(
        self,
    ) -> None:
        ticket = "T-110"
        ticket_path = self.product / f"factory/tickets/{ticket}.md"
        ticket_path.parent.mkdir(parents=True)
        existing = self.product / "apps/existing.test.ts"
        existing.parent.mkdir()
        existing.write_text("test\n", encoding="utf-8")
        ticket_path.write_text(
            f"# {ticket}\n\nState: Planning\n"
            "Product-Decisions: frozen\n"
            "Builder ownership: apps/feature.ts only\n"
            "Fixture-Seams: apps/planned.test.ts\n"
            "Authentication-Seams: apps/existing.test.ts\n"
            "Protected-Test-Conflicts: none\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "init", "-q", "-b", f"ticket/{ticket}"],
            cwd=self.product, check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"], cwd=self.product,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=self.product, check=True,
        )
        subprocess.run(["git", "add", "."], cwd=self.product, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "role-authored contract"],
            cwd=self.product, check=True,
        )
        source = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.product, text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        self.operator_passport(
            ticket, "Planning", "none", head_sha=source,
        )
        passport_path = self.state / f"passports/{ticket}.json"
        transition = {
            "branch": f"ticket/{ticket}", "consumed": False,
            "contract_version": "1.8.0", "factory_sha": self.release.name,
            "head_sha": source,
            "lease_sha256": hashlib.sha256(("a" * 64).encode()).hexdigest(),
            "passport_sha256": hashlib.sha256(
                passport_path.read_bytes()
            ).hexdigest(),
            "project": "relay", "role": "planner",
            "route_plan_sha256": "d" * 64,
            "schema": "nysa.software-factory.transition-receipt/v1",
            "stage": "RUN planner", "ticket": ticket,
        }
        transition["receipt_sha256"] = hashlib.sha256(STATE.canonical({
            key: value for key, value in transition.items()
            if key not in {"consumed", "receipt_sha256"}
        })).hexdigest()
        CONTROL.write(self.state / f"{ticket}.json", transition)
        controller = CONTROL.Controller(self.args)
        controller.event(
            "preflight_refused", ticket,
            preflight_exit_code=1,
            preflight_failure_lines=["PREFLIGHT FAIL: planned fixture"],
            preflight_output_sha256="e" * 64,
            preflight_reason_code="deterministic_refusal",
            transition_receipt_sha256=transition["receipt_sha256"],
        )
        event = next(controller.events.glob("*.json"))
        event_digest = CONTROL.read(event)["event_sha256"]
        claim = {
            "blocked_reason": "preflight", "branch": f"ticket/{ticket}",
            "lease": "a" * 64, "lease_released": True,
            "publication_lease": "", "receipt": "", "role": "",
            "schema": CONTROL.CLAIM_SCHEMA, "status": "blocked",
            "ticket": ticket, "worktree": str(self.product),
        }
        controller.remote_cell_head_status = lambda _claim: (
            "pushed", source, source,
        )
        for _tick in range(7):
            controller.recover_each(
                [claim], controller.recover_passport_preflight_blocks,
                "preflight-retry",
            )
        self.assertNotIn("recovery_attempt", claim)
        self.assertEqual(claim["receipt"], transition["receipt_sha256"])
        corrected = ticket_path.read_text(encoding="utf-8").replace(
            "Fixture-Seams: apps/planned.test.ts",
            "Fixture-Seams: apps/existing.test.ts",
        ) + (
            f"\nOPERATOR PREFLIGHT RECEIPT: {transition['receipt_sha256']}\n"
            f"OPERATOR PREFLIGHT FAILURE EVENT: {event_digest}\n"
        )
        ticket_path.write_text(corrected, encoding="utf-8")
        subprocess.run(["git", "add", str(ticket_path)], cwd=self.product, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "correct preflight contract"],
            cwd=self.product, check=True,
        )
        target = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.product, text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        (self.release / "scripts").mkdir()
        shutil.copy(ROOT / "scripts/ticket-readiness.py", self.release / "scripts")
        self.assertTrue(controller.preflight_correction_valid(
            claim, source, target, transition["receipt_sha256"], {event_digest},
        ))

        calls = []
        state_calls = 0

        class InjectedCrash(BaseException):
            pass

        def json_call(*arguments, **_kwargs):
            nonlocal state_calls
            calls.append(arguments[0])
            if arguments[0] == "claim":
                lease = "b" * 64 if state_calls == 0 else "c" * 64
                return {
                    "lease_id": lease, "schema_version": 1,
                    "ticket": ticket,
                }
            if arguments[0] == "state-machine":
                state_calls += 1
                self.operator_passport(
                    ticket, "Planning", "none", head_sha=target,
                )
                current = CONTROL.read(self.state / f"{ticket}.json")
                successor = {
                    **current, "head_sha": target,
                    "lease_sha256": hashlib.sha256(
                        claim["lease"].encode()
                    ).hexdigest(),
                    "parent_digest": current["receipt_sha256"],
                    "passport_sha256": hashlib.sha256(
                        passport_path.read_bytes()
                    ).hexdigest(),
                }
                successor["receipt_sha256"] = hashlib.sha256(STATE.canonical({
                    key: value for key, value in successor.items()
                    if key not in {"consumed", "receipt_sha256"}
                })).hexdigest()
                CONTROL.write(self.state / f"{ticket}.json", successor)
                if state_calls == 1:
                    raise InjectedCrash("after successor receipt")
                return state_transition(
                    "RUN planner", successor["receipt_sha256"], ticket,
                )
            if arguments[0] == "preflight":
                return {"exit_code": 0, "status": "ok"}
            raise AssertionError(arguments)

        controller.json_call = json_call
        controller.remote_cell_head_status = lambda _claim: (
            "pushed", target, target,
        )
        controller.remote_passport_valid = lambda _claim: True
        with self.assertRaisesRegex(InjectedCrash, "successor receipt"):
            controller.recover_passport_preflight_blocks([claim])
        self.assertEqual(claim["receipt"], transition["receipt_sha256"])
        claim["lease_released"] = True
        controller.recover_passport_preflight_blocks([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(calls, [
            "claim", "state-machine", "claim", "state-machine", "preflight",
        ])
        recovered = [
            CONTROL.read(path) for path in controller.events.glob("*.json")
            if CONTROL.read(path).get("event") == "passport_preflight_recovered"
        ]
        self.assertEqual(len(recovered), 1)

    def test_passport_preflight_recovery_resumes_authenticated_frontier(
        self,
    ) -> None:
        ticket = "T-110"
        source = "b" * 40
        target = "c" * 40
        self.operator_passport(ticket, "Planning", "none", head_sha=source)
        passport_path = self.state / f"passports/{ticket}.json"
        passport = CONTROL.read(passport_path)
        passport.pop("authentication_sha256")
        passport.pop("passport_sha256")
        passport["completed_role_evidence"] = [
            {
                "contract_version": "1.8.0",
                "factory_sha": self.release.name,
                "head_before": source,
                "manifest_sha256": str(index) * 64,
                "output_sha256": str(index + 2) * 64,
                "role": role,
                "run_id": f"run-{role}",
                "transition_receipt_sha256": str(index + 4) * 64,
            }
            for index, role in enumerate(("planner", "spec-linter"), 1)
        ]
        passport = PASSPORT.authenticate(
            passport, (self.state / "passport.key").read_bytes(),
        )
        PASSPORT.write_atomic(passport_path, passport)
        transition = {
            "branch": f"ticket/{ticket}", "consumed": False,
            "contract_version": "1.8.0", "factory_sha": self.release.name,
            "head_sha": source,
            "lease_sha256": hashlib.sha256(("a" * 64).encode()).hexdigest(),
            "passport_sha256": hashlib.sha256(
                passport_path.read_bytes()
            ).hexdigest(),
            "project": "relay", "role": "planner",
            "route_plan_sha256": "d" * 64,
            "schema": "nysa.software-factory.transition-receipt/v1",
            "stage": "RUN planner", "ticket": ticket,
        }
        transition["receipt_sha256"] = hashlib.sha256(STATE.canonical({
            key: value for key, value in transition.items()
            if key not in {"consumed", "receipt_sha256"}
        })).hexdigest()
        CONTROL.write(self.state / f"{ticket}.json", transition)
        controller = CONTROL.Controller(self.args)
        controller.event(
            "preflight_refused", ticket,
            preflight_exit_code=1,
            preflight_failure_lines=["PREFLIGHT FAIL: stale planner receipt"],
            preflight_output_sha256="e" * 64,
            preflight_reason_code="deterministic_refusal",
            transition_receipt_sha256=transition["receipt_sha256"],
        )
        claim = {
            "blocked_reason": "preflight", "branch": f"ticket/{ticket}",
            "lease": "a" * 64, "lease_released": True,
            "publication_lease": "", "receipt": "", "role": "",
            "schema": CONTROL.CLAIM_SCHEMA, "status": "blocked",
            "ticket": ticket, "worktree": str(self.product),
        }
        controller.remote_cell_head_status = lambda _claim: (
            "pushed", target, target,
        )
        controller.preflight_correction_valid = lambda *_args: True
        controller.remote_passport_valid = lambda _claim: True
        controller.ensure_lease = lambda item, _reason: item.update(
            lease="f" * 64, lease_released=False,
        )
        controller.release_ticket_lease = lambda item: item.update(
            lease_released=True,
        )
        preflight_roles = []

        def json_call(*arguments, **_kwargs):
            if arguments[0] == "state-machine":
                current = {
                    **transition, "head_sha": target,
                    "lease_sha256": hashlib.sha256(
                        claim["lease"].encode()
                    ).hexdigest(),
                    "parent_digest": transition["receipt_sha256"],
                    "role": "test-author", "stage": "RUN test-author",
                }
                current["receipt_sha256"] = hashlib.sha256(STATE.canonical({
                    key: value for key, value in current.items()
                    if key not in {"consumed", "receipt_sha256"}
                })).hexdigest()
                CONTROL.write(self.state / f"{ticket}.json", current)
                return state_transition(
                    "RUN test-author", current["receipt_sha256"], ticket,
                )
            if arguments[0] == "preflight":
                preflight_roles.append(arguments[arguments.index("--role") + 1])
                return {"exit_code": 0, "status": "ok"}
            raise AssertionError(arguments)

        controller.json_call = json_call
        controller.recover_passport_preflight_blocks([claim])

        self.assertEqual(preflight_roles, ["test-author"])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["role"], "")

    def test_corrected_passportless_preflight_block_reopens_fail_closed(self) -> None:
        tickets = ["T-110", "T-111", "T-112"]
        (self.product / "factory/PROJECT.env").write_text(
            "MAX_CONCURRENT_TICKETS=3\n", encoding="utf-8"
        )
        (self.product / "factory/QUALIFICATION.json").write_text(json.dumps({
            "budget_usd": "100.000000",
            "capacity": 3,
            "contract_version": "1.8.0",
            "factory_sha": "a" * 40,
            "generation": 1,
            "per_run_budget_usd": "2.000000",
            "per_ticket_budget_usd": "25.000000",
            "schema": CONTROL.QUALIFICATION_SCHEMA,
            "target_done": 3,
            "tickets": tickets,
        }), encoding="utf-8")
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        cell.mkdir()
        claim = {
            "blocked_reason": "preflight",
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "lease_released": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        self.operator_transition("T-110", "RUN planner", role="planner")
        transition = {
            "action": "RUN",
            "detail": "planner",
            "loop": None,
            "receipt": "c" * 64,
            "role": "planner",
            "schema": "nysa.software-factory.state-machine/v1",
            "stage": "RUN planner",
            "status": "ok",
            "ticket": "T-110",
        }
        calls = []

        def json_call(*args, **_kwargs):
            calls.append(args[0])
            if args[0] == "claim":
                return {"lease_id": "d" * 64, "schema_version": 1, "ticket": "T-110"}
            if args[0] == "state-machine":
                return transition
            if args[0] == "preflight":
                return {"exit_code": 0, "status": "ok"}
            raise AssertionError(args)

        controller.json_call = json_call
        controller.ticket_release_current = lambda _claim: True
        controller.remote_cell_head_valid = lambda _claim: True
        with (
            patch.object(CONTROL, "ensure_qualification_artifacts"),
            patch.object(
                CONTROL.subprocess, "run",
                return_value=argparse.Namespace(stdout=""),
            ),
        ):
            controller.recover_preflight_blocks([claim])

        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["lease"], "d" * 64)
        self.assertNotIn("blocked_reason", claim)
        self.assertNotIn("lease_released", claim)
        self.assertEqual(calls, ["claim", "state-machine", "preflight"])

        (self.product / "factory/QUALIFICATION.json").unlink()
        production = CONTROL.Controller(self.args)
        claim.update(
            status="blocked", blocked_reason="preflight", lease_released=True,
        )
        calls.clear()
        production.json_call = json_call
        production.ticket_release_current = lambda _claim: True
        production.remote_cell_head_valid = lambda _claim: True
        with (
            patch.object(
                CONTROL, "ensure_qualification_artifacts",
                side_effect=AssertionError("production used qualification artifacts"),
            ),
            patch.object(
                CONTROL.subprocess, "run",
                return_value=argparse.Namespace(stdout=""),
            ),
        ):
            production.recover_preflight_blocks([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(calls, ["claim", "state-machine", "preflight"])

        claim.update(
            status="blocked", blocked_reason="preflight", lease_released=True,
        )
        controller.release_ticket_lease = lambda item: item.update(lease_released=True)
        controller.json_call = lambda *args, **_kwargs: (
            {"lease_id": "e" * 64, "schema_version": 1, "ticket": "T-110"}
            if args[0] == "claim"
            else transition if args[0] == "state-machine"
            else {
                "exit_code": 1,
                "output": "PREFLIGHT FAIL: deterministic fixture refusal\n",
                "status": "error",
            }
        )
        with (
            patch.object(CONTROL, "ensure_qualification_artifacts"),
            patch.object(
                CONTROL.subprocess, "run",
                return_value=argparse.Namespace(stdout=""),
            ),
        ):
            controller.recover_preflight_blocks([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(claim["blocked_reason"], "preflight")
        self.assertTrue(claim["lease_released"])

    def test_three_passportless_worker_errors_retry_preflight_once(self) -> None:
        tickets = ["T-170", "T-171", "T-172"]
        controller, claims = self.initialize_passportless_planner_claims(tickets)
        controller.capacity = 3
        recovered = []

        def ensure_lease(claim, label):
            recovered.append((claim["ticket"], label))
            if claim.get("lease_released") is True:
                claim["lease"] = hashlib.sha256(
                    f"replacement-{claim['ticket']}".encode()
                ).hexdigest()
                claim.pop("lease_released")

        def json_call(*args, **_kwargs):
            ticket = args[args.index("--ticket") + 1]
            if args[0] == "state-machine":
                return state_transition(
                    "RUN planner", hashlib.sha256(ticket.encode()).hexdigest(), ticket,
                )
            if args[0] == "preflight":
                return {"exit_code": 0, "status": "ok"}
            raise AssertionError(args)

        controller.ensure_lease = ensure_lease
        controller.json_call = json_call
        controller.recover_each(
            claims, controller.recover_preflight_blocks,
            "preflight-retry", concurrent=True,
        )
        controller.recover_each(
            claims, controller.recover_preflight_blocks,
            "preflight-retry", concurrent=True,
        )

        self.assertEqual(
            sorted(recovered),
            [(ticket, "preflight-retry") for ticket in tickets],
        )
        self.assertEqual([claim["status"] for claim in claims], ["claimed"] * 3)
        self.assertTrue(all("blocked_reason" not in claim for claim in claims))
        events = [
            CONTROL.read(path) for path in controller.events.glob("*.json")
            if CONTROL.read(path).get("event")
            == "preflight_worker_error_recovered"
        ]
        self.assertEqual(sorted(item["ticket"] for item in events), tickets)

    def test_passportless_kit_refusal_recovers_after_exact_route_migration(self) -> None:
        controller, claims = self.initialize_passportless_planner_claims(["T-177"])
        claim = claims[0]
        fallback = self.install_passportless_fallback(claim)
        receipt_path = self.state / "T-177.json"
        receipt = CONTROL.read(receipt_path)
        successor = self.root / ("e" * 40)
        successor.mkdir()
        receipt.update(
            factory_sha=successor.name,
            role=None,
            stage="REFUSE ticket Kit-SHA lease does not match the selected kit SHA",
        )
        immutable = {
            key: value for key, value in receipt.items()
            if key not in {"consumed", "consumed_at_epoch", "receipt_sha256"}
        }
        receipt["receipt_sha256"] = hashlib.sha256(
            STATE.canonical(immutable)
        ).hexdigest()
        receipt_path.unlink()
        CONTROL.write(receipt_path, receipt)

        worktree = Path(claim["worktree"])
        ticket = worktree / "factory/tickets/T-177.md"
        ticket.write_text(ticket.read_text().replace(self.release.name, successor.name))
        (worktree / "factory/KIT_PIN").write_text(successor.name + "\n")
        route = worktree / "factory/route-plans/T-177.json"
        catalog, routes, _profiles, profile_map = ROUTER.load_policy()
        value = MANAGER.migrate_v2_journal(
            fallback, receipt["head_sha"], successor.name,
            "2026-08-07T00:01:00Z", catalog, routes, profile_map,
        )
        route.write_text(ROUTER.canonical_json(value) + "\n")
        subprocess.run(["git", "-C", worktree, "add", "factory"], check=True)
        subprocess.run(
            [
                "git", "-C", worktree, "-c", "user.name=Software Factory",
                "-c", "user.email=factory@local", "commit", "-qm",
                "migrate route",
            ], check=True,
        )
        subprocess.run(
            ["git", "-C", worktree, "push", "-q", "origin", "ticket/T-177"],
            check=True,
        )
        controller.release_path = successor
        claim.update(
            blocked_reason="state-machine-refusal", lease_released=True,
        )
        calls = []
        crash = [True]

        def ensure_lease(item, label):
            calls.append(label)
            item.pop("lease_released", None)
            if crash.pop() if crash else False:
                raise KeyboardInterrupt("crash after durable lease recovery")

        controller.ensure_lease = ensure_lease
        with self.assertRaises(KeyboardInterrupt):
            controller.recover_passportless_route_migrations([claim])
        self.assertEqual(claim["status"], "blocked")
        controller.recover_passportless_route_migrations([claim])
        controller.recover_passportless_route_migrations([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertNotIn("blocked_reason", claim)
        self.assertEqual(calls, ["passportless-route-migration"] * 2)

    def test_passportless_kit_refusal_rejects_untrusted_control_bytes(self) -> None:
        controller, claims = self.initialize_passportless_planner_claims(["T-177"])
        claim = claims[0]
        fallback = self.install_passportless_fallback(claim)
        receipt = CONTROL.read(self.state / "T-177.json")
        successor = self.root / ("e" * 40)
        successor.mkdir()
        receipt.update(
            factory_sha=successor.name, role=None,
            stage="REFUSE ticket Kit-SHA lease does not match the selected kit SHA",
        )
        immutable = {
            key: value for key, value in receipt.items()
            if key not in {"consumed", "consumed_at_epoch", "receipt_sha256"}
        }
        receipt["receipt_sha256"] = hashlib.sha256(
            STATE.canonical(immutable)
        ).hexdigest()
        (self.state / "T-177.json").unlink()
        CONTROL.write(self.state / "T-177.json", receipt)
        worktree = Path(claim["worktree"])
        ticket = worktree / "factory/tickets/T-177.md"
        ticket.write_text(
            ticket.read_text().replace(self.release.name, successor.name)
            + "\nAcceptance-Criteria: untrusted rewrite\n"
        )
        route = worktree / "factory/route-plans/T-177.json"
        catalog, routes, _profiles, profile_map = ROUTER.load_policy()
        value = MANAGER.migrate_v2_journal(
            fallback, receipt["head_sha"], successor.name,
            "2026-08-07T00:01:00Z", catalog, routes, profile_map,
        )
        route.write_text(ROUTER.canonical_json(value) + "\n")
        subprocess.run(["git", "-C", worktree, "add", "factory"], check=True)
        subprocess.run(["git", "-C", worktree, "commit", "-qm", "unsafe migration"], check=True)
        subprocess.run(["git", "-C", worktree, "push", "-q", "origin", "ticket/T-177"], check=True)
        controller.release_path = successor
        claim.update(blocked_reason="state-machine-refusal", lease_released=True)
        controller.recover_passportless_route_migrations([claim])
        self.assertEqual(claim["status"], "blocked")

    def test_passportless_kit_refusal_rejects_untrusted_fallback_snapshot(self) -> None:
        controller, claims = self.initialize_passportless_planner_claims(["T-177"])
        claim = claims[0]
        fallback = self.install_passportless_fallback(
            claim, "app/untrusted-sibling.py",
        )
        receipt_path = self.state / "T-177.json"
        receipt = CONTROL.read(receipt_path)
        successor = self.root / ("e" * 40)
        successor.mkdir()
        receipt.update(
            factory_sha=successor.name, role=None,
            stage="REFUSE ticket Kit-SHA lease does not match the selected kit SHA",
        )
        immutable = {
            key: value for key, value in receipt.items()
            if key not in {"consumed", "consumed_at_epoch", "receipt_sha256"}
        }
        receipt["receipt_sha256"] = hashlib.sha256(
            STATE.canonical(immutable)
        ).hexdigest()
        receipt_path.unlink()
        CONTROL.write(receipt_path, receipt)
        worktree = Path(claim["worktree"])
        ticket = worktree / "factory/tickets/T-177.md"
        ticket.write_text(ticket.read_text().replace(self.release.name, successor.name))
        route = worktree / "factory/route-plans/T-177.json"
        catalog, routes, _profiles, profile_map = ROUTER.load_policy()
        value = MANAGER.migrate_v2_journal(
            fallback, receipt["head_sha"], successor.name,
            "2026-08-07T00:01:00Z", catalog, routes, profile_map,
        )
        route.write_text(ROUTER.canonical_json(value) + "\n")
        subprocess.run(["git", "-C", worktree, "add", "factory"], check=True)
        subprocess.run(
            ["git", "-C", worktree, "commit", "-qm", "migrate route"], check=True,
        )
        subprocess.run(
            ["git", "-C", worktree, "push", "-q", "origin", "ticket/T-177"],
            check=True,
        )
        controller.release_path = successor
        claim.update(blocked_reason="state-machine-refusal", lease_released=True)
        controller.recover_passportless_route_migrations([claim])
        self.assertEqual(claim["status"], "blocked")

    def test_passportless_kit_refusal_rejects_intermediate_kit_mismatch(self) -> None:
        controller, claims = self.initialize_passportless_planner_claims(["T-177"])
        claim = claims[0]
        fallback = self.install_passportless_fallback(claim)
        receipt_path = self.state / "T-177.json"
        receipt = CONTROL.read(receipt_path)
        middle, wrong, final = "d" * 40, "e" * 40, "f" * 40
        receipt.update(
            factory_sha=final, role=None,
            stage="REFUSE ticket Kit-SHA lease does not match the selected kit SHA",
        )
        immutable = {
            key: value for key, value in receipt.items()
            if key not in {"consumed", "consumed_at_epoch", "receipt_sha256"}
        }
        receipt["receipt_sha256"] = hashlib.sha256(
            STATE.canonical(immutable)
        ).hexdigest()
        receipt_path.unlink()
        CONTROL.write(receipt_path, receipt)

        worktree = Path(claim["worktree"])
        ticket = worktree / "factory/tickets/T-177.md"
        route = worktree / "factory/route-plans/T-177.json"
        catalog, routes, _profiles, profile_map = ROUTER.load_policy()
        first = MANAGER.migrate_v2_journal(
            fallback, receipt["head_sha"], middle,
            "2026-08-07T00:01:00Z", catalog, routes, profile_map,
        )
        ticket.write_text(ticket.read_text().replace(self.release.name, wrong))
        route.write_text(ROUTER.canonical_json(first) + "\n")
        subprocess.run(["git", "-C", worktree, "add", "factory"], check=True)
        subprocess.run(
            ["git", "-C", worktree, "commit", "-qm", "mismatched migration"],
            check=True,
        )
        second = MANAGER.migrate_v2_journal(
            first, receipt["head_sha"], final,
            "2026-08-07T00:02:00Z", catalog, routes, profile_map,
        )
        ticket.write_text(ticket.read_text().replace(wrong, final))
        route.write_text(ROUTER.canonical_json(second) + "\n")
        subprocess.run(["git", "-C", worktree, "add", "factory"], check=True)
        subprocess.run(
            ["git", "-C", worktree, "commit", "-qm", "final migration"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", worktree, "push", "-q", "origin", "ticket/T-177"],
            check=True,
        )
        successor = self.root / final
        successor.mkdir()
        controller.release_path = successor
        claim.update(blocked_reason="state-machine-refusal", lease_released=True)
        controller.recover_passportless_route_migrations([claim])
        self.assertEqual(claim["status"], "blocked")

    def test_passportless_worker_error_rejects_identity_and_runtime_drift(self) -> None:
        controller, claims = self.initialize_passportless_planner_claims(["T-170"])
        claim = claims[0]
        receipt_path = self.state / "T-170.json"
        original = CONTROL.read(receipt_path)
        calls = []
        controller.ensure_lease = lambda *_args: calls.append("lease")
        controller.ticket_release_current = lambda _claim: True
        controller.remote_cell_head_valid = lambda _claim: True

        def attempt(receipt=None):
            CONTROL.write(receipt_path, receipt or original)
            controller.recover_preflight_blocks([claim])
            self.assertEqual(claim["status"], "blocked")
            self.assertEqual(calls, [])

        for field, value in (
            ("consumed", True),
            ("ticket", "T-171"),
            ("factory_sha", "b" * 40),
            ("head_sha", "b" * 40),
            ("lease_sha256", "b" * 64),
            ("product_origin_sha256", "b" * 64),
            ("route_plan_sha256", "b" * 64),
        ):
            with self.subTest(field=field):
                changed = {**original, field: value}
                immutable = {
                    key: item for key, item in changed.items()
                    if key not in {
                        "consumed", "consumed_at_epoch", "receipt_sha256",
                    }
                }
                changed["receipt_sha256"] = hashlib.sha256(
                    STATE.canonical(immutable)
                ).hexdigest()
                attempt(changed)

        passport = self.state / "passports/T-170.json"
        passport.parent.mkdir(mode=0o700)
        CONTROL.write(passport, {})
        attempt()
        passport.unlink()

        controller.role_active = lambda _claim: True
        attempt()
        controller.role_active = lambda _claim: False

        run = self.product / "factory/runs/pre-provider.meta"
        run.write_text("ticket=T-170\n", encoding="utf-8")
        attempt()
        run.unlink()

        controller.remote_cell_head_valid = lambda _claim: False
        attempt()
        controller.remote_cell_head_valid = lambda _claim: True

        dirty = Path(claim["worktree"]) / "dirty"
        dirty.write_text("dirty\n", encoding="utf-8")
        attempt()
        dirty.unlink()

        foreign = self.root / "foreign"
        subprocess.run(["git", "clone", "-q", str(self.root / "origin.git"), str(foreign)], check=True)
        subprocess.run(
            ["git", "-C", str(foreign), "checkout", "-q", "ticket/T-170"],
            check=True,
        )
        foreign_claim = {**claim, "worktree": str(foreign)}
        self.assertFalse(
            controller.exact_passportless_planner_receipt(foreign_claim, original)
        )
        route = controller.route_path(claim)
        route.write_text("[]\n", encoding="utf-8")
        self.assertFalse(
            controller.exact_passportless_planner_receipt(claim, original)
        )

    def test_passportless_preflight_retry_rejects_unsafe_boundaries(self) -> None:
        tickets = ["T-110", "T-111", "T-112"]
        (self.product / "factory/PROJECT.env").write_text(
            "MAX_CONCURRENT_TICKETS=3\n", encoding="utf-8"
        )
        (self.product / "factory/QUALIFICATION.json").write_text(json.dumps({
            "budget_usd": "100.000000",
            "capacity": 3,
            "contract_version": "1.8.0",
            "factory_sha": "a" * 40,
            "generation": 1,
            "per_run_budget_usd": "2.000000",
            "per_ticket_budget_usd": "25.000000",
            "schema": CONTROL.QUALIFICATION_SCHEMA,
            "target_done": 3,
            "tickets": tickets,
        }), encoding="utf-8")
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        cell.mkdir()
        claim = {
            "blocked_reason": "preflight",
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "lease_released": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        CONTROL.write(self.state / "T-110.json", {
            "branch": "ticket/T-110",
            "consumed": False,
            "receipt_sha256": "b" * 64,
            "role": "planner",
            "schema": "nysa.software-factory.transition-receipt/v1",
            "stage": "RUN planner",
            "ticket": "T-110",
        })
        leases = []
        controller.ensure_lease = lambda *_args: leases.append("lease")
        controller.ticket_release_current = lambda _claim: True
        controller.remote_cell_head_valid = lambda _claim: True

        passport = self.state / "passports/T-110.json"
        passport.parent.mkdir(mode=0o700)
        CONTROL.write(passport, {})
        controller.recover_preflight_blocks([claim])
        passport.unlink()

        run = self.product / "factory/runs/preflight.meta"
        run.write_text("ticket=T-110\n", encoding="utf-8")
        controller.recover_preflight_blocks([claim])
        run.unlink()

        with patch.object(
            CONTROL.subprocess, "run",
            return_value=argparse.Namespace(stdout=" M factory/tickets/T-110.md"),
        ):
            controller.recover_preflight_blocks([claim])

        controller.remote_cell_head_valid = lambda _claim: False
        with patch.object(
            CONTROL.subprocess, "run",
            return_value=argparse.Namespace(stdout=""),
        ):
            controller.recover_preflight_blocks([claim])

        controller.role_active = lambda _claim: True
        controller.recover_preflight_blocks([claim])
        self.assertEqual(leases, [])

    def test_narrator_receives_trusted_publication_context(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        cell.mkdir()
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        captured = []

        class CompletedProcess:
            def __init__(self, command, **_kwargs):
                captured.append(command)

            @staticmethod
            def wait(timeout=None):
                return 0

        controller.ensure_execution_cell = lambda _claim: None
        controller.terminal_for_receipt = lambda *_args: {}
        controller.finish_pending_run = lambda _claim: True
        publication = {
            "checks": [],
            "head": "b" * 40,
            "pr_number": 7,
            "publication_mode": "railway",
            "preview_urls": [
                "https://api-example-pr-7.up.railway.app",
                "https://web-example-pr-7.up.railway.app",
            ],
            "status": "ready",
            "url": "https://github.com/example/product/pull/7",
        }
        with patch.object(CONTROL.subprocess, "Popen", CompletedProcess):
            controller.run_role(
                claim, "narrator", "c" * 64, [], publication,
            )
        task = captured[0][-1]
        self.assertIn("PR #7", task)
        self.assertIn("web-example-pr-7.up.railway.app", task)
        self.assertIn("Do not run tests", task)

    def test_narrator_receives_trusted_nonvisual_publication_context(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-nonvisual"
        cell.mkdir()
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        captured = []

        class CompletedProcess:
            def __init__(self, command, **_kwargs):
                captured.append(command)

            @staticmethod
            def wait(timeout=None):
                return 0

        controller.ensure_execution_cell = lambda _claim: None
        controller.terminal_for_receipt = lambda *_args: {}
        controller.finish_pending_run = lambda _claim: True
        publication = {
            "checks": [],
            "head": "b" * 40,
            "pr_number": 7,
            "publication_mode": "nonvisual",
            "preview_identity": {
                "expected": "b" * 40,
                "observed": [{
                    "paths_sha256": "d" * 64,
                    "policy": "nonvisual_paths",
                }],
                "reason": None,
                "status": "pass",
            },
            "preview_urls": [],
            "status": "ready",
            "url": "https://github.com/example/product/pull/7",
        }
        with patch.object(CONTROL.subprocess, "Popen", CompletedProcess):
            controller.run_role(
                claim, "narrator", "c" * 64, [], publication,
            )
        task = captured[0][-1]
        self.assertIn("FACTORY_PR_NONVISUAL_EVIDENCE_V1", task)
        self.assertIn("Mark Preview and Screenshots not applicable", task)
        self.assertNotIn("up.railway.app", task)

    def test_role_launch_without_terminal_blocks_once(self) -> None:
        controller = CONTROL.Controller(self.args)
        exit_statuses = [0, 3]
        releases = []

        class MissingTerminalProcess:
            def __init__(self, *_args, **_kwargs):
                pass

            @staticmethod
            def wait(timeout=None):
                return exit_statuses.pop(0)

        controller.ensure_execution_cell = lambda _claim: None
        def release_ticket_lease(claim):
            releases.append(claim["ticket"])
            if claim["ticket"] == "T-111":
                raise CONTROL.ControllerError("launch lock stuck")

        controller.release_ticket_lease = release_ticket_lease
        with patch.object(CONTROL.subprocess, "Popen", MissingTerminalProcess):
            for number in (110, 111):
                ticket = f"T-{number}"
                cell = self.root / f"cell-{number}"
                cell.mkdir()
                receipt = f"{number:064x}"
                claim = {
                    "branch": f"ticket/{ticket}",
                    "lease": "a" * 64,
                    "priority": "normal",
                    "publication_lease": "",
                    "receipt": "",
                    "role": "",
                    "schema": CONTROL.CLAIM_SCHEMA,
                    "status": "claimed",
                    "ticket": ticket,
                    "worktree": str(cell),
                }
                controller.run_role(claim, "builder", receipt, [])
                self.assertEqual(claim["status"], "blocked")
                self.assertEqual(claim["receipt"], receipt)
                self.assertEqual(claim["role"], "builder")

        self.assertEqual(releases, ["T-110", "T-111"])
        events = [
            CONTROL.read(path) for path in sorted(self.state.glob("events/*.json"))
        ]
        missing = [
            item for item in events
            if item["event"] == "role_launch_missing_terminal"
        ]
        self.assertEqual(
            [
                (item["ticket"], item["exit_status"], item["cleanup_deferred"])
                for item in missing
            ],
            [("T-110", 0, []), ("T-111", 3, ["lease"])],
        )

    def test_qualification_missing_terminal_latches_after_process(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {"generation": 1, "tickets": ["T-110"]}
        claim = {
            "branch": "ticket/T-110", "lease": "a" * 64,
            "publication_lease": "", "receipt": "", "role": "",
            "schema": CONTROL.CLAIM_SCHEMA, "status": "claimed",
            "ticket": "T-110", "worktree": str(self.root / "cell-1"),
        }
        Path(claim["worktree"]).mkdir()
        controller.ensure_execution_cell = lambda _claim: None
        controller.terminal_for_receipt = lambda *_args: None
        controller.release_ticket_lease = lambda *_args: None

        class MissingTerminalProcess:
            def __init__(self, *_args, **_kwargs):
                pass

            @staticmethod
            def wait(timeout=None):
                return 0

        with (
            patch.object(CONTROL, "ensure_qualification_artifacts"),
            patch.object(CONTROL.subprocess, "Popen", MissingTerminalProcess),
        ):
            self.assertTrue(
                controller.run_role(claim, "builder", "b" * 64, [])
            )

        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(claim["blocked_reason"], "missing-terminal")
        self.assertTrue(controller.qualification_cohort_error.is_set())

    def test_delayed_terminal_is_finished_without_rerunning_role(self) -> None:
        controller = CONTROL.Controller(self.args)
        receipt = "b" * 64
        claim = {
            "blocked_reason": "missing-terminal",
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "lease_released": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": receipt,
            "role": "builder",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        terminal = {
            "kit_sha": self.release.name,
            "role": "builder",
            "run_id": "delayed-terminal",
        }
        terminals = [None, terminal]
        controller.terminal_for_receipt = lambda *_args: terminals.pop(0)
        controller.role_active = lambda _claim: False
        recovered = []
        controller.ensure_lease = lambda _claim, label: recovered.append(label)
        controller.finish_pending_run = lambda item: item.update(
            receipt="", role="", status="claimed"
        )

        controller.recover_missing_terminals([claim])
        self.assertEqual(claim["status"], "blocked")
        controller.recover_missing_terminals([claim])

        self.assertEqual(recovered, ["missing-terminal"])
        self.assertEqual(claim["status"], "claimed")
        events = [CONTROL.read(path) for path in controller.events.glob("*.json")]
        event = next(item for item in events if item["event"] == "missing_terminal_recovered")
        self.assertEqual(event["run_id"], "delayed-terminal")
        self.assertEqual(event["transition_receipt_sha256"], receipt)

    def test_missing_terminal_recovery_rejects_unsafe_boundaries(self) -> None:
        controller = CONTROL.Controller(self.args)
        base_claim = {
            "blocked_reason": "missing-terminal",
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "lease_released": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "b" * 64,
            "role": "builder",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        base_terminal = {
            "kit_sha": self.release.name,
            "role": "builder",
            "run_id": "delayed-terminal",
        }
        cases = {
            "malformed receipt": ({"receipt": "invalid"}, {}, False),
            "unknown role": ({"role": "unknown"}, {}, False),
            "unreleased lease": ({"lease_released": False}, {}, False),
            "active role": ({}, {}, True),
            "wrong terminal role": ({}, {"role": "reviewer"}, False),
            "wrong terminal kit": ({}, {"kit_sha": "f" * 40}, False),
        }
        for name, (claim_patch, terminal_patch, active) in cases.items():
            with self.subTest(name=name):
                claim = {**base_claim, **claim_patch}
                terminal = {**base_terminal, **terminal_patch}
                calls = []
                controller.role_active = lambda _claim, value=active: value
                controller.terminal_for_receipt = lambda *_args, value=terminal: value
                controller.ensure_lease = lambda *_args: calls.append("lease")
                controller.finish_pending_run = lambda *_args: calls.append("finish")

                controller.recover_missing_terminals([claim])

                self.assertEqual(calls, [])
                self.assertEqual(claim["status"], "blocked")

    def test_model_pin_relies_on_its_bounded_probes_not_an_outer_timeout(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        cell.mkdir()
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        calls = []

        def json_call(*args, **kwargs):
            calls.append((args, kwargs))
            route = cell / "factory/route-plans/T-110.json"
            route.parent.mkdir(parents=True)
            route.write_text("{}\n", encoding="utf-8")
            return {
                "pins": [{}], "schema": "model-pin-batch/v1", "status": "ok",
            }

        controller.json_call = json_call
        self.assertEqual(controller.pin_routes([claim]), [])
        model_calls = [
            (args, kwargs)
            for args, kwargs in calls
            if args[:2] == ("models", "pin-batch")
        ]
        self.assertEqual(len(model_calls), 1)
        self.assertIsNone(model_calls[0][1]["timeout"])

    def test_state_machine_relies_on_its_bounded_helpers_not_an_outer_timeout(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        calls = []

        def json_call(*args, **kwargs):
            calls.append((args, kwargs))
            if args[0] == "state-machine":
                transition = state_transition("FIX builder")
                transition["loop"] = {
                    "attempt": 2,
                    "capped": False,
                    "kind": "builder-reviewer",
                    "limit": 3,
                }
                return transition
            if args[:2] == ("publication", "withdraw"):
                return {"status": "absent"}
            raise AssertionError(args)

        controller.json_call = json_call
        controller.ensure_lease = lambda *_args: None
        controller.finish_pending_run = lambda _claim: True
        controller.refresh_dependency_tracking = lambda _claim: True
        controller.run_role = lambda *_args: None

        self.assertEqual(
            controller.reconcile_ticket(claim),
            {"status": "progressed", "ticket": "T-110"},
        )
        state_machine_calls = [
            (args, kwargs)
            for args, kwargs in calls
            if args[0] == "state-machine"
        ]
        self.assertEqual(len(state_machine_calls), 1)
        self.assertIn("timeout", state_machine_calls[0][1])
        self.assertIsNone(state_machine_calls[0][1]["timeout"])
        loop_events = [
            json.loads(path.read_text())
            for path in controller.events.glob("*.json")
            if json.loads(path.read_text()).get("event") == "loop_attempt"
        ]
        self.assertEqual(len(loop_events), 1)
        self.assertEqual(loop_events[0]["attempt"], 2)
        self.assertEqual(loop_events[0]["stage"], "FIX builder")

    def test_state_machine_transition_envelope_mutations_fail_before_provider(
        self,
    ) -> None:
        cell = self.root / "cell-1"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        valid = {
            "action": "FIX",
            "detail": "builder",
            "receipt": "b" * 64,
            "role": "builder",
            "schema": "nysa.software-factory.state-machine/v1",
            "stage": "FIX builder",
            "status": "ok",
            "ticket": "T-110",
        }
        cases = {
            "schema": {**valid, "schema": "wrong"},
            "status": {**valid, "status": "error"},
            "ticket": {**valid, "ticket": "T-111"},
            "action": {**valid, "action": "RUN"},
            "detail": {**valid, "detail": "narrator"},
            "receipt": {**valid, "receipt": "not-a-digest"},
            "empty-stage": {**valid, "stage": "", "action": "", "detail": None},
            "unknown-stage": {
                **valid, "stage": "GARBAGE", "action": "GARBAGE",
                "detail": None, "role": None,
            },
            "role-stage-mismatch": {**valid, "role": "narrator"},
            "non-role-has-role": {
                **valid,
                "action": "AWAIT-OPERATOR",
                "detail": "product decision required",
                "role": "builder",
                "stage": "AWAIT-OPERATOR product decision required",
            },
            "loop-extra-key": {
                **valid,
                "loop": {
                    "attempt": 1, "capped": False, "extra": True,
                    "kind": "builder-reviewer", "limit": 3,
                },
            },
            "loop-zero-attempt": {
                **valid,
                "loop": {
                    "attempt": 0, "capped": False,
                    "kind": "builder-reviewer", "limit": 3,
                },
            },
        }

        for name, transition in cases.items():
            with self.subTest(mutation=name):
                controller = CONTROL.Controller(self.args)
                claim = {
                    "branch": "ticket/T-110",
                    "lease": "a" * 64,
                    "priority": "normal",
                    "publication_lease": "",
                    "receipt": "",
                    "role": "",
                    "schema": CONTROL.CLAIM_SCHEMA,
                    "status": "claimed",
                    "ticket": "T-110",
                    "worktree": str(cell),
                }
                controller.ensure_lease = lambda *_args: None
                controller.finish_pending_run = lambda _claim: True
                controller.refresh_dependency_tracking = lambda _claim: True
                controller.json_call = lambda *_args, **_kwargs: transition
                controller.withdraw_publication = lambda *_args: None
                controller.release_ticket_lease = lambda item: item.update(
                    lease="", lease_released=True
                )
                controller.run_role = lambda *_args: self.fail(
                    f"{name} reached provider execution"
                )
                controller.ticket_pr = lambda *_args: self.fail(
                    f"{name} reached publication execution"
                )

                result = controller.reconcile_ticket(claim)

                self.assertEqual(result["status"], "error")
                self.assertEqual(
                    result["error"],
                    "state-machine returned invalid transition evidence",
                )
                self.assertEqual(claim["status"], "blocked")
                self.assertTrue(claim["lease_released"])

    def test_concurrent_tickets_share_one_batch_readiness_probe(self) -> None:
        controller = CONTROL.Controller(self.args)
        claims = []
        for number in range(4):
            cell = self.root / f"cell-{number + 1}"
            cell.mkdir()
            claims.append({
                "branch": f"ticket/T-{110 + number}",
                "lease": f"{number + 1:064x}",
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "claimed",
                "ticket": f"T-{110 + number}",
                "worktree": str(cell),
            })
        calls = []

        def json_call(*args, **_kwargs):
            calls.append(args)
            for claim in claims:
                route = controller.route_path(claim)
                route.parent.mkdir(parents=True, exist_ok=True)
                route.write_text("{}\n", encoding="utf-8")
            return {
                "pins": [{} for _ in claims],
                "schema": "model-pin-batch/v1",
                "status": "ok",
            }

        controller.json_call = json_call
        self.assertEqual(controller.pin_routes(claims), [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ("models", "pin-batch"))
        self.assertEqual(
            [calls[0][index] for index in range(3, len(calls[0]), 4)],
            [claim["ticket"] for claim in claims],
        )

    def test_temporary_model_outage_waits_four_tickets_after_one_probe(self) -> None:
        controller = CONTROL.Controller(self.args)
        claims = []
        for number in range(4):
            cell = self.root / f"cell-{number + 1}"
            cell.mkdir()
            claims.append({
                "branch": f"ticket/T-{110 + number}",
                "lease": f"{number + 1:064x}",
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "claimed",
                "ticket": f"T-{110 + number}",
                "worktree": str(cell),
            })
        model_calls = 0

        def json_call(*args, **_kwargs):
            nonlocal model_calls
            if args[:2] == ("models", "pin-batch"):
                model_calls += 1
                return self.model_resolution_error(
                    "profile_temporarily_unavailable", operation="pin",
                )
            raise AssertionError("state machine must wait for model readiness")

        controller.json_call = json_call
        results = controller.pin_routes(claims)

        self.assertEqual(model_calls, 1)
        self.assertEqual({item["status"] for item in results}, {"waiting"})
        self.assertEqual({item["status"] for item in claims}, {"waiting"})

    def test_blocked_ticket_is_excluded_without_holding_capacity(self) -> None:
        controller = CONTROL.Controller(self.args)
        blocked = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        calls = []

        def dispatch(*arguments, **_kwargs):
            calls.append(arguments)
            return {"action": "WAIT"}

        controller.json_call = dispatch
        controller.claim_new([blocked])
        self.assertEqual(
            calls,
            [(
                "dispatch-plan", "--shadow", "--exclude-ticket", "T-110", "--json"
            )],
        )

    def test_qualification_cursor_failure_routes_to_direct_cli(self) -> None:
        (self.product / "factory/QUALIFICATION.json").write_text(
            json.dumps({
                "budget_usd": "100.000000",
                "capacity": 4,
                "contract_version": "1.8.0",
                "factory_sha": "a" * 40,
                "generation": 1,
                "per_run_budget_usd": "2.000000",
                "per_ticket_budget_usd": "25.000000",
                "schema": CONTROL.QUALIFICATION_SCHEMA,
                "target_done": 4,
                "tickets": ["T-110", "T-111", "T-112", "T-113"],
            }),
            encoding="utf-8",
        )
        controller = CONTROL.Controller(self.args)
        claim = {
            "lease": "a" * 64,
            "publication_lease": "",
            "receipt": "b" * 64,
            "role": "planner",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "running",
            "ticket": "T-112",
            "worktree": str(self.root / "cell-3"),
        }
        (self.product / "factory/runs/failed.meta").write_text(
            "run_id=failed\n"
            "ticket=T-112\n"
            "role=planner\n"
            "route_id=cursor-gpt\n"
            "task_submitted=1\n"
            "accounting_state=abandoned_conservative\n"
            "exit_status=9\n"
            "role_exit=provider_failed\n"
            f"transition_receipt_sha256={'b' * 64}\n",
            encoding="utf-8",
        )
        calls = []

        class FallbackGuard:
            def __enter__(self):
                calls.append("fallback-lock")

            def __exit__(self, *_args):
                return False

        controller.fallback_lock = FallbackGuard()
        controller.passport = lambda *_args: calls.append("passport")
        controller.json_call = lambda *args, **_kwargs: (
            calls.append(args) or {"failed_run_id": "failed"}
        )
        controller.migrate_passport = lambda *_args: calls.append("migrate")
        controller.event = lambda *_args, **_kwargs: calls.append("event")
        self.assertTrue(controller.finish_pending_run(claim))
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["receipt"], "")
        self.assertIn(
            (
                "models", "fallback-auto", "--ticket", "T-112",
                "--failed-run", "failed", "--workdir", claim["worktree"],
                "--reason", "provider_unavailable", "--json",
            ),
            calls,
        )
        self.assertIn("fallback-lock", calls)
        self.assertNotIn("passport", calls)
        fallback = next(
            index
            for index, call in enumerate(calls)
            if isinstance(call, tuple) and call[:2] == ("models", "fallback-auto")
        )
        self.assertLess(fallback, calls.index("migrate"))
        self.assertFalse(
            any(
                isinstance(call, tuple) and call and call[0] == "release"
                for call in calls
            )
        )
        self.assertFalse(controller.qualification_cohort_error.is_set())

        claim.update(
            lease="a" * 64, receipt="b" * 64, role="planner", status="running",
        )
        typed = []
        controller.json_call = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            CONTROL.ControllerError(
                '{"error":"automatic qualification fallback refused:manifest",'
                '"status":"error"}'
            )
        )
        controller.block = lambda item, reason: item.update(
            blocked_reason=reason, status="blocked",
        )
        controller.release_ticket_lease = lambda *_args: calls.append("released")
        controller.event_once = lambda *args, **kwargs: typed.append((args, kwargs))
        self.assertFalse(controller.finish_pending_run(claim))
        self.assertEqual(
            claim["blocked_reason"],
            "qualification-fallback-refused:manifest:" + "a" * 40,
        )
        self.assertIn("released", calls)
        self.assertEqual(typed[0][0], ("typed_recovery_refused", "T-112"))
        self.assertEqual(typed[0][1]["reason"], "manifest")
        self.assertEqual(typed[0][1]["recovery_kind"], "qualification_fallback")
        self.assertTrue(controller.qualification_cohort_error.is_set())
        calls.clear()
        controller.restore_recorded_contract_repair = lambda _claim: False
        controller.restore_contract_blocker = lambda _claim: False
        controller.role_active = lambda _claim: False
        controller.recover_repaired_failures([claim])
        self.assertNotIn("provider-fallback-recovery", calls)
        self.assertFalse(
            any(
                isinstance(call, tuple) and call[:2] == ("models", "fallback-auto")
                for call in calls
            )
        )

    def test_qualification_unsubmitted_failure_exports_passport(self) -> None:
        (self.product / "factory/QUALIFICATION.json").write_text(
            json.dumps({
                "budget_usd": "100.000000",
                "capacity": 4,
                "contract_version": "1.8.0",
                "factory_sha": "a" * 40,
                "generation": 1,
                "per_run_budget_usd": "2.000000",
                "per_ticket_budget_usd": "25.000000",
                "schema": CONTROL.QUALIFICATION_SCHEMA,
                "target_done": 4,
                "tickets": ["T-110", "T-111", "T-112", "T-113"],
            }),
            encoding="utf-8",
        )
        controller = CONTROL.Controller(self.args)
        claim = {
            "lease": "a" * 64,
            "publication_lease": "",
            "receipt": "b" * 64,
            "role": "planner",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "running",
            "ticket": "T-112",
            "worktree": str(self.root / "cell-3"),
        }
        (self.product / "factory/runs/failed.meta").write_text(
            "run_id=failed\n"
            "ticket=T-112\n"
            "role=planner\n"
            "route_id=cursor-gpt\n"
            "task_submitted=0\n"
            "accounting_state=abandoned_conservative\n"
            "exit_status=125\n"
            "role_exit=provider_failed\n"
            f"transition_receipt_sha256={'b' * 64}\n",
            encoding="utf-8",
        )
        calls = []
        controller.emit_attempt_terminal = lambda *_args: None
        controller.passport = lambda *_args: calls.append("passport")
        controller.json_call = lambda *args, **_kwargs: calls.append(args)
        controller.save_claim = lambda *_args: None
        controller.release_ticket_lease = lambda *_args: calls.append("release")
        controller.event = lambda *_args, **_kwargs: calls.append("event")

        self.assertFalse(controller.finish_pending_run(claim))
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(claim["blocked_reason"], "role-failure")
        self.assertIn("passport", calls)
        self.assertNotIn("migrate", calls)
        self.assertFalse(any(
            isinstance(call, tuple) and call[:2] == ("models", "fallback-auto")
            for call in calls
        ))

    def test_reconcile_rearms_exact_externally_applied_fallback(self) -> None:
        (self.product / "factory/QUALIFICATION.json").write_text(
            json.dumps({
                "budget_usd": "100.000000",
                "capacity": 4,
                "contract_version": "1.8.0",
                "factory_sha": "a" * 40,
                "generation": 1,
                "per_run_budget_usd": "2.000000",
                "per_ticket_budget_usd": "25.000000",
                "schema": CONTROL.QUALIFICATION_SCHEMA,
                "target_done": 4,
                "tickets": ["T-110", "T-111", "T-112", "T-113"],
            }),
            encoding="utf-8",
        )
        controller = CONTROL.Controller(self.args)
        claim = {
            "branch": "ticket/T-112",
            "lease": "",
            "parked": True,
            "publication_lease": "",
            "receipt": "b" * 64,
            "role": "reviewer",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-112",
            "worktree": str(self.root / "parked/T-112"),
        }
        (self.product / "factory/runs/failed-reviewer.meta").write_text(
            "run_id=failed-reviewer\n"
            "ticket=T-112\n"
            "role=reviewer\n"
            "route_id=cursor-reviewer\n"
            "task_submitted=1\n"
            "accounting_state=abandoned_conservative\n"
            "exit_status=6\n"
            "role_exit=provider_failed\n"
            f"transition_receipt_sha256={'b' * 64}\n",
            encoding="utf-8",
        )
        calls = []
        controller.restore_recorded_contract_repair = lambda _claim: False
        controller.restore_contract_blocker = lambda _claim: False
        controller.role_active = lambda _claim: False
        def json_call(*args, **_kwargs):
            calls.append(args)
            if args[0] == "claim":
                return {
                    "lease_id": "c" * 64,
                    "schema_version": 1,
                    "ticket": "T-112",
                }
            return {"failed_run_id": "failed-reviewer"}

        controller.json_call = json_call
        controller.renew = lambda _claim: None

        def migrate(_claim, _publication):
            self.assertEqual(_claim["receipt"], "b" * 64)
            self.assertEqual(_claim["role"], "reviewer")
            calls.append("migrate")

        controller.migrate_passport = migrate
        controller.event = lambda name, *_args, **_kwargs: calls.append(name)
        controller.recover_repaired_failures([claim])

        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["lease"], "c" * 64)
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["role"], "")
        self.assertLess(calls.index("migrate"), calls.index("provider_fallback"))

        claim.update(status="blocked", receipt="b" * 64, role="reviewer")
        controller.json_call = lambda *_args, **_kwargs: {
            "failed_run_id": "different-run"
        }
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "did not bind the failed run"
        ):
            controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(claim["receipt"], "b" * 64)
        self.assertEqual(claim["role"], "reviewer")

    def test_invalid_reviewer_output_retries_only_reviewer(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {"tickets": ["T-110"]}
        claim = {
            "lease": "a" * 64,
            "publication_lease": "",
            "receipt": "b" * 64,
            "role": "reviewer",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "running",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        (self.product / "factory/runs/invalid.meta").write_text(
            "run_id=invalid\n"
            "ticket=T-110\n"
            "role=reviewer\n"
            "accounting_state=abandoned_conservative\n"
            "exit_status=11\n"
            "role_exit=role_exit_invalid_output\n"
            f"transition_receipt_sha256={'b' * 64}\n",
            encoding="utf-8",
        )
        calls = []
        controller.passport = lambda *_args: calls.append("passport")
        controller.migrate_passport = lambda *_args: calls.append("migrate")
        controller.event = lambda name, *_args, **_kwargs: calls.append(name)
        self.assertTrue(controller.finish_pending_run(claim))
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["role"], "")
        self.assertEqual(
            calls, [
                "attempt_terminal", "passport", "migrate",
                "role_output_rejected",
            ]
        )
        self.assertFalse(controller.qualification_cohort_error.is_set())

    def test_contract_block_waits_for_exact_resume_then_reclaims(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        cell.mkdir()
        receipt = "b" * 64
        head = "c" * 40
        passport_digest = "d" * 64
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": receipt,
            "role": "planner",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "running",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        (self.product / "factory/runs/blocked.meta").write_text(
            "run_id=blocked\n"
            "ticket=T-110\n"
            "role=planner\n"
            "accounting_state=abandoned_conservative\n"
            "exit_status=12\n"
            "role_exit=role_exit_contract_blocked\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )
        calls = []
        controller.passport = lambda *_args: calls.append("passport")
        controller.migrate_passport = lambda *_args: calls.append("migrate")

        def json_call(*args, **_kwargs):
            calls.append(args)
            if args[:2] == ("state-machine", "repair-check"):
                return {
                    "action": "repair-check", "head": "c" * 40,
                    "role": "planner",
                    "schema": "nysa.software-factory.state-machine/v1",
                    "status": "ready", "ticket": "T-110",
                }
            if args[:2] == ("state-machine", "block"):
                return {"status": "blocked"}
            return {}

        controller.json_call = json_call
        self.assertFalse(controller.finish_pending_run(claim))
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(
            [call if isinstance(call, str) else call[:2] for call in calls],
            [
                "passport", ("state-machine", "block"),
                ("release", "--ticket"),
            ],
        )

        (self.state / "passports").mkdir(mode=0o700)
        CONTROL.write(
            self.state / "passports/T-110.json",
            {
                "branch": claim["branch"],
                "head_sha": head,
                "passport_sha256": passport_digest,
            },
        )
        ticket = cell / "factory/tickets/T-110.md"
        ticket.parent.mkdir(parents=True)
        ticket.write_text(
            f"# T-110\n\nOPERATOR RESUME RECEIPT: {'f' * 64}\n",
            encoding="utf-8",
        )
        calls.clear()
        resume_status = "waiting"

        def recover_call(*args, **_kwargs):
            calls.append(args)
            if args[:2] == ("state-machine", "block"):
                return {"status": "blocked"}
            if args[:2] == ("state-machine", "resume"):
                if resume_status == "error":
                    return {
                        "actual_bytes": 120,
                        "expected_bytes": 80,
                        "first_differing_line": 5,
                        "reason_code": "resume_commit_content_mismatch",
                        "status": "error",
                    }
                return {"status": resume_status}
            if args[:2] == ("passport", "validate"):
                return {"passport": passport_digest, "status": "ok"}
            if args[0] == "renew":
                raise CONTROL.ControllerError("released")
            if args[0] == "claim":
                return {
                    "lease_id": "e" * 64,
                    "schema_version": 1,
                    "ticket": "T-110",
                }
            return {}

        controller.json_call = recover_call
        controller.event = lambda name, *_args, **_kwargs: calls.append((name,))
        remote = CONTROL.subprocess.CompletedProcess(
            [], 0, f"{head}\trefs/heads/{claim['branch']}\n", ""
        )
        with patch.object(
            CONTROL.subprocess, "run",
            side_effect=lambda command, **_kwargs: (
                CONTROL.subprocess.CompletedProcess(command, 0, "", "")
                if "status" in command else remote
            ),
        ):
            controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertIn(("claim", "--ticket", "T-110"), calls)
        self.assertIn(("ticket_lease_recovered",), calls)
        self.assertNotIn(
            ("state-machine", "resume"), [call[:2] for call in calls]
        )

        ticket.write_text(
            f"# T-110\n\nOPERATOR RESUME: planner\n"
            f"OPERATOR RESUME RECEIPT: {receipt}\n",
            encoding="utf-8",
        )
        cell_status = "resume_commit_not_pushed"
        controller.remote_cell_head_status = lambda _claim: (
            cell_status, head, "a" * 40
        )
        calls.clear()
        with patch.object(CONTROL.subprocess, "run", return_value=remote):
            controller.recover_repaired_failures([claim])
        self.assertIn(("contract_resume_refused",), calls)
        self.assertNotIn(
            ("state-machine", "resume"), [call[:2] for call in calls]
        )

        calls.clear()
        cell_status = "pushed"
        with patch.object(CONTROL.subprocess, "run", return_value=remote):
            controller.recover_repaired_failures([claim])
        self.assertIn(("state-machine", "resume"), [call[:2] for call in calls])

        calls.clear()
        resume_status = "error"
        controller.prior_transition_tickets.add("T-110")
        with patch.object(CONTROL.subprocess, "run", return_value=remote):
            controller.recover_repaired_failures([claim])
        self.assertIn(("contract_resume_refused",), calls)
        self.assertEqual(claim["status"], "blocked")
        self.assertIn("T-110", controller.prior_transition_tickets)

        calls.clear()
        resume_status = "ready"
        with patch.object(CONTROL.subprocess, "run", return_value=remote):
            controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["lease"], "e" * 64)
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["role"], "")
        self.assertIn(("contract_blocker_recovered",), calls)
        self.assertNotIn("T-110", controller.prior_transition_tickets)

    def test_normalized_contract_block_exports_before_block_transition(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-normalized-block"
        cell.mkdir()
        receipt = "b" * 64
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "publication_lease": "",
            "receipt": receipt,
            "role": "builder",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "running",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        (self.product / "factory/runs/normalized-block.meta").write_text(
            "run_id=normalized-block\n"
            "ticket=T-110\n"
            "role=builder\n"
            "accounting_state=abandoned_conservative\n"
            "exit_status=12\n"
            "role_exit=role_exit_contract_blocked\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )
        calls = []
        controller.passport = lambda *_args: calls.append("normalized-export")

        def json_call(*args, **_kwargs):
            if args[:2] == ("state-machine", "block"):
                self.assertEqual(calls, ["normalized-export"])
                calls.append("state-machine-block")
                return {"status": "blocked"}
            if args[0] == "release":
                calls.append("release")
            return {}

        controller.json_call = json_call
        self.assertFalse(controller.finish_pending_run(claim))
        self.assertEqual(
            calls, ["normalized-export", "state-machine-block", "release"]
        )
        self.assertEqual(claim["status"], "blocked")

    def test_recorded_repair_recovers_after_transition_receipt_was_replaced(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        cell.mkdir()
        claim = {
            "branch": "ticket/T-110",
            "lease": "",
            "parked": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        repairs = self.state / "contract-repairs"
        repairs.mkdir(mode=0o700)
        CONTROL.write(repairs / "T-110.json", {"authenticated": "by-state-machine"})
        calls = []

        def json_call(*args, **_kwargs):
            calls.append(args)
            if args[0] == "claim":
                return {
                    "lease_id": "e" * 64,
                    "schema_version": 1,
                    "ticket": "T-110",
                }
            return {}

        controller.json_call = json_call
        controller.remote_passport_valid = lambda _claim: True
        controller.event = lambda name, *_args, **kwargs: calls.append(
            (name, kwargs)
        )
        controller.prior_transition_tickets.add("T-110")

        controller.recover_repaired_failures([claim])

        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["lease"], "e" * 64)
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["role"], "")
        self.assertIn(
            (
                "recorded_contract_repair_prepared",
                {},
            ),
            calls,
        )
        self.assertNotIn("T-110", controller.prior_transition_tickets)
        self.assertFalse(any(call[0] == "state-machine" for call in calls))

        prior = {
            "branch": claim["branch"],
            "consumed": True,
            "contract_version": "2.0.0",
            "factory_sha": "b" * 40,
            "project": "relay",
            "schema": "nysa.software-factory.transition-receipt/v1",
            "ticket": "T-110",
        }
        prior["receipt_sha256"] = hashlib.sha256(
            CONTROL.canonical_document({
                key: value for key, value in prior.items()
                if key not in {"consumed", "receipt_sha256"}
            })
        ).hexdigest()
        CONTROL.write(self.state / "T-110.json", prior)
        claim.pop("parked")
        claim["recovery_attempt"] = {
            "count": 0,
            "factory_sha": self.release.name,
            "input_sha256": controller.recovery_input_sha256(
                claim, "targeted-repair",
            ),
            "outcome_sha256": "",
            "phase": "pending", "recovery": "targeted-repair",
            "retry_reason": "", "retry_status": "blocked",
        }
        controller.save_claim(claim)
        (repairs / "T-110.json").unlink()
        restarted = CONTROL.Controller(self.args)
        persisted = restarted.load_claims()[0]
        self.assertIsNone(restarted.operator_transition(persisted))
        self.assertIn("T-110", restarted.prior_transition_tickets)
        restarted.remote_passport_valid = lambda _claim: False
        restarted.recover_each(
            [persisted], restarted.recover_repaired_failures,
            "targeted-repair",
        )
        self.assertEqual(persisted["status"], "claimed")
        self.assertEqual(persisted["recovery_attempt"]["phase"], "pending")
        self.assertIn("T-110", restarted.prior_transition_tickets)
        restarted.remote_passport_valid = lambda _claim: True
        restarted.recover_each(
            [persisted], restarted.recover_repaired_failures,
            "targeted-repair",
        )
        self.assertEqual(persisted["status"], "claimed")
        self.assertEqual(persisted["recovery_attempt"]["phase"], "pending")
        self.assertNotIn("T-110", restarted.prior_transition_tickets)

    def test_recorded_repair_recovers_claim_left_at_blocked_terminal(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        cell.mkdir()
        blocked_receipt = "d" * 64
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "parked": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": blocked_receipt,
            "role": "builder",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        repairs = self.state / "contract-repairs"
        repairs.mkdir(mode=0o700)
        CONTROL.write(repairs / "T-110.json", {
            "blocked_receipt": blocked_receipt,
            "blocked_role": "builder",
            "repair_role": "test-author",
        })
        calls = []

        def json_call(*args, **_kwargs):
            calls.append(args)
            return {}

        controller.json_call = json_call
        controller.remote_passport_valid = lambda _claim: True
        controller.event = lambda name, *_args, **kwargs: calls.append(
            (name, kwargs)
        )

        controller.recover_repaired_failures([claim])

        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["lease"], "a" * 64)
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["role"], "")
        self.assertIn(
            (
                "recorded_contract_repair_prepared",
                {},
            ),
            calls,
        )
        self.assertFalse(any(call[0] == "state-machine" for call in calls))

    def test_recorded_repair_refuses_mismatched_blocked_claim(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        cell.mkdir()
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "parked": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "d" * 64,
            "role": "builder",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        repairs = self.state / "contract-repairs"
        repairs.mkdir(mode=0o700)
        CONTROL.write(repairs / "T-110.json", {
            "blocked_receipt": "e" * 64,
            "blocked_role": "builder",
            "repair_role": "test-author",
        })
        calls = []
        controller.json_call = lambda *args, **_kwargs: calls.append(args)
        controller.remote_passport_valid = lambda _claim: True
        controller.prior_transition_tickets.add("T-110")

        self.assertFalse(controller.restore_recorded_contract_repair(claim))

        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(claim["receipt"], "d" * 64)
        self.assertEqual(claim["role"], "builder")
        self.assertIn("T-110", controller.prior_transition_tickets)
        self.assertEqual(calls, [])

    def test_invalid_recorded_repair_fails_in_the_single_ordinary_resolution(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        cell.mkdir()
        claim = {
            "branch": "ticket/T-110",
            "lease": "",
            "parked": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        repairs = self.state / "contract-repairs"
        repairs.mkdir(mode=0o700)
        CONTROL.write(repairs / "T-110.json", {"invalid": True})
        calls = []

        def json_call(*args, **_kwargs):
            calls.append(args)
            if args[0] == "claim":
                return {
                    "lease_id": "e" * 64,
                    "schema_version": 1,
                    "ticket": "T-110",
                }
            if args[0] == "state-machine":
                raise CONTROL.ControllerError("repair record is invalid")
            if args[:2] == ("publication", "withdraw"):
                return {"status": "absent"}
            return {}

        controller.json_call = json_call
        controller.remote_passport_valid = lambda _claim: True
        controller.event = lambda *_args, **_kwargs: None

        self.assertTrue(controller.restore_recorded_contract_repair(claim))

        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["lease"], "e" * 64)
        self.assertFalse(any(call[0] == "state-machine" for call in calls))

        (cell / "factory/route-plans").mkdir(parents=True)
        (cell / "factory/route-plans/T-110.json").write_text(
            '{"ticket":"T-110"}\n', encoding="utf-8"
        )
        controller.finish_pending_run = lambda _claim: True
        controller.refresh_dependency_tracking = lambda _claim: True
        controller.ticket_merged = lambda _claim: False
        controller.run_role = lambda *_args: self.fail(
            "invalid repair evidence reached a provider role"
        )

        result = controller.reconcile_ticket(claim)

        self.assertEqual(result["status"], "error")
        self.assertEqual(claim["status"], "blocked")
        self.assertTrue(claim["lease_released"])
        self.assertEqual(
            len([call for call in calls if call[0] == "state-machine"]), 1
        )
        self.assertIn(
            ("release", "--ticket", "T-110", "--lease", "e" * 64),
            calls,
        )

    def test_composite_historical_state_replay(self) -> None:
        fixture_path = ROOT / "ci/fixtures/composite-historical-state.json"
        fixture_raw = fixture_path.read_bytes()
        fixture = json.loads(fixture_raw)
        key = hashlib.sha256(b"sanitized-composite-replay-key").digest()
        primary = fixture["primary"]
        source = fixture["release"]["source"]
        successor_a = fixture["release"]["successor_a"]
        successor_b = fixture["release"]["successor_b"]

        def digest(value) -> str:
            return hashlib.sha256(STATE.canonical(value)).hexdigest()

        def oid(label: str) -> str:
            return hashlib.sha256(label.encode()).hexdigest()[:40]

        def require(condition: bool, message: str) -> None:
            if not condition:
                raise ValueError(message)

        charges = []
        completed = []
        for number, (role, result, factory_sha) in enumerate(
            primary["charged_attempts"], 1
        ):
            run_id = f"historical-{number:02d}"
            receipt = digest(["receipt", run_id])
            charge = {
                "amount": 10_000_000,
                "factory_sha": factory_sha,
                "receipt": receipt,
                "role": role,
                "run_id": run_id,
            }
            charges.append(charge)
            if result == "ok":
                completed.append({
                    **charge,
                    "output": digest(["output", run_id]),
                })

        initial_head = oid("historical-ticket-head")
        initial_tree = oid("historical-ticket-tree")
        route_revisions = []
        route_parent = None
        for number in range(primary["initial_route_revision_count"]):
            body = {"factory_sha": source, "kind": "historical", "number": number}
            revision_hash = ATTEST.route_revision_hash(number, route_parent, body)
            route_revisions.append({
                "body": body,
                "parent_hash": route_parent,
                "revision": number,
                "revision_hash": revision_hash,
            })
            route_parent = revision_hash

        migrations = []
        migration_parent = None
        for number in range(primary["initial_migration_count"]):
            body = {
                "from_factory_sha": source,
                "from_head_sha": initial_head,
                "to_factory_sha": source,
                "to_head_sha": initial_head,
            }
            edge_hash = digest({
                "body": body,
                "parent_hash": migration_parent,
                "revision": number,
            })
            migrations.append({
                **body,
                "edge_hash": edge_hash,
                "parent_hash": migration_parent,
                "revision": number,
            })
            migration_parent = edge_hash

        charged_factories = list(dict.fromkeys(
            item["factory_sha"] for item in charges
        ))
        release_history = [
            *charged_factories,
            *[
                oid(f"historical-release-{number}")
                for number in range(
                    primary["initial_release_count"] - len(charged_factories) - 1
                )
            ],
            source,
        ]
        blocker_receipt = charges[-1]["receipt"]
        lease = digest("initial-lease")
        state = {
            "activation": {"active_factory_sha": source, "history": [source]},
            "claim": {
                "branch": primary["branch"],
                "cell": "/sealed/cells/primary",
                "head_sha": initial_head,
                "lease": lease,
                "passport_sha256": "",
                "parked": True,
                "status": "blocked",
                "ticket": primary["ticket"],
                "transition_receipt": blocker_receipt,
            },
            "conflict": {
                "blob": digest("protected-test-blob"),
                "mode": "100644",
                "path": "tests/dependency.test.ts",
                "repair_owner": "test-author",
            },
            "passport": {
                "base_sha": oid("protected-main-before-dependency"),
                "branch": primary["branch"],
                "charges": charges,
                "completed": completed,
                "cumulative_charges_micro_usd": 130_000_000,
                "factory_sha": source,
                "factory_release_history": release_history,
                "head_sha": initial_head,
                "migration_history": migrations,
                "parent_digest": migration_parent,
                "publication_state": "none",
                "route_revision_hash": route_parent,
                "ticket": primary["ticket"],
                "tree_sha": initial_tree,
            },
            "publication": {
                "approval_head": None,
                "merge_lease": None,
                "queue": [],
                "reviewed_head": None,
                "state": "none",
                "tested_head": None,
            },
            "receipt": {
                "base_sha": oid("protected-main-before-dependency"),
                "digest": blocker_receipt,
                "factory_sha": source,
                "head_sha": initial_head,
                "lease_sha256": hashlib.sha256(lease.encode()).hexdigest(),
                "route_revision_hash": route_parent,
                "tree_sha": initial_tree,
            },
            "receipt_history": [],
            "repair": {
                "blocked_receipt": blocker_receipt,
                "directive_receipt": blocker_receipt,
                "owner": primary["repair_owner"],
                "status": "pending",
            },
            "route": {"revisions": route_revisions},
            "tickets": {
                primary["ticket"]: {"provider": False, "publication": False},
                fixture["tickets"]["dependents"][0]: {
                    "dependency": primary["ticket"], "parked": True,
                    "provider": False, "publication": False, "status": "waiting",
                },
                fixture["tickets"]["dependents"][1]: {
                    "dependency": primary["ticket"], "parked": True,
                    "provider": False, "publication": False, "status": "waiting",
                },
                fixture["tickets"]["dormant"]: {
                    "provider": False, "publication": False, "status": "backlog",
                },
                fixture["tickets"]["independent_sibling"]: {
                    "charges": [], "completed": [], "provider": True,
                    "publication": False, "status": "running",
                },
            },
        }
        expected_conflict = copy.deepcopy(state["conflict"])

        def seal(value: dict) -> None:
            passport = value["passport"]
            value["claim"]["passport_sha256"] = digest(passport)
            unsigned = {
                name: item for name, item in value.items()
                if name != "authentication_sha256"
            }
            value["authentication_sha256"] = hmac.new(
                key, STATE.canonical(unsigned), hashlib.sha256
            ).hexdigest()

        def validate(value: dict) -> None:
            unsigned = {
                name: item for name, item in value.items()
                if name != "authentication_sha256"
            }
            require(
                hmac.compare_digest(
                    value.get("authentication_sha256", ""),
                    hmac.new(key, STATE.canonical(unsigned), hashlib.sha256).hexdigest(),
                ),
                "aggregate HMAC mismatch",
            )
            passport = value["passport"]
            claim = value["claim"]
            receipt = value["receipt"]
            route = value["route"]["revisions"]
            parent = None
            for number, revision in enumerate(route):
                require(revision["revision"] == number, "route revision gap")
                require(revision["parent_hash"] == parent, "route parent mismatch")
                require(
                    revision["revision_hash"] == ATTEST.route_revision_hash(
                        number, parent, revision["body"]
                    ),
                    "route revision hash mismatch",
                )
                parent = revision["revision_hash"]
            require(parent == passport["route_revision_hash"], "route tip mismatch")
            migration_parent = None
            for number, edge in enumerate(passport["migration_history"]):
                body = {
                    name: edge[name] for name in (
                        "from_factory_sha", "from_head_sha",
                        "to_factory_sha", "to_head_sha",
                    )
                }
                require(edge["revision"] == number, "passport migration gap")
                require(edge["parent_hash"] == migration_parent, "migration parent mismatch")
                require(
                    edge["edge_hash"] == digest({
                        "body": body,
                        "parent_hash": migration_parent,
                        "revision": number,
                    }),
                    "passport migration edge mismatch",
                )
                migration_parent = edge["edge_hash"]
            require(passport["parent_digest"] == migration_parent, "passport parent mismatch")
            require(claim["passport_sha256"] == digest(passport), "claim passport mismatch")
            require(claim["ticket"] == passport["ticket"], "ticket identity mismatch")
            require(claim["branch"] == passport["branch"], "branch identity mismatch")
            require(claim["head_sha"] == passport["head_sha"] == receipt["head_sha"], "head mismatch")
            require(passport["tree_sha"] == receipt["tree_sha"], "tree mismatch")
            require(passport["base_sha"] == receipt["base_sha"], "base mismatch")
            require(
                passport["route_revision_hash"] == receipt["route_revision_hash"],
                "receipt route mismatch",
            )
            require(claim["transition_receipt"] == receipt["digest"], "receipt mismatch")
            require(
                receipt["lease_sha256"] == hashlib.sha256(
                    claim["lease"].encode()
                ).hexdigest(),
                "lease mismatch",
            )
            require(
                passport["factory_sha"] == receipt["factory_sha"]
                == value["activation"]["active_factory_sha"],
                "active Factory mismatch",
            )
            require(
                passport["factory_release_history"][-1] == passport["factory_sha"],
                "release history tip mismatch",
            )
            require(
                all(
                    charge["factory_sha"] in passport["factory_release_history"]
                    for charge in passport["charges"]
                ),
                "historical charge Factory changed",
            )
            run_ids = [item["run_id"] for item in passport["charges"]]
            receipts = [item["receipt"] for item in passport["charges"]]
            outputs = [item["output"] for item in passport["completed"]]
            require(len(run_ids) == len(set(run_ids)), "duplicate charge run")
            require(len(receipts) == len(set(receipts)), "duplicate charged receipt")
            require(len(outputs) == len(set(outputs)), "duplicate role output")
            require(
                passport["cumulative_charges_micro_usd"]
                == sum(item["amount"] for item in passport["charges"]),
                "charge accounting mismatch",
            )
            require(
                {item["run_id"] for item in passport["completed"]}
                <= set(run_ids),
                "successful role lacks a charge",
            )
            require(
                value["repair"]["directive_receipt"]
                == value["repair"]["blocked_receipt"],
                "repair directive receipt mismatch",
            )
            require(value["conflict"] == expected_conflict, "unsafe conflict evidence")
            publication = value["publication"]
            if publication["state"] == "merged":
                require(publication["queue"] == [], "publication queue not drained")
                require(
                    publication["reviewed_head"] == publication["tested_head"]
                    == publication["approval_head"] == passport["head_sha"],
                    "publication head mismatch",
                )

        def new_receipt(label: str) -> None:
            state["receipt_history"].append(copy.deepcopy(state["receipt"]))
            state["receipt"] = {
                "base_sha": state["passport"]["base_sha"],
                "digest": digest(["transition", label, len(state["receipt_history"])]),
                "factory_sha": state["passport"]["factory_sha"],
                "head_sha": state["passport"]["head_sha"],
                "lease_sha256": hashlib.sha256(
                    state["claim"]["lease"].encode()
                ).hexdigest(),
                "route_revision_hash": state["passport"]["route_revision_hash"],
                "tree_sha": state["passport"]["tree_sha"],
            }
            state["claim"]["transition_receipt"] = state["receipt"]["digest"]

        def passport_migration(
            label: str, *, factory_sha: str | None = None,
            head_sha: str | None = None, tree_sha: str | None = None,
            route_change: bool = False,
        ) -> None:
            passport = state["passport"]
            old_factory = passport["factory_sha"]
            old_head = passport["head_sha"]
            target_factory = factory_sha or old_factory
            target_head = head_sha or old_head
            if route_change:
                number = len(state["route"]["revisions"])
                parent = state["route"]["revisions"][-1]["revision_hash"]
                body = {
                    "kind": "release-migration",
                    "old_factory_sha": old_factory,
                    "new_factory_sha": target_factory,
                }
                revision_hash = ATTEST.route_revision_hash(number, parent, body)
                state["route"]["revisions"].append({
                    "body": body, "parent_hash": parent,
                    "revision": number, "revision_hash": revision_hash,
                })
                passport["route_revision_hash"] = revision_hash
            number = len(passport["migration_history"])
            parent = passport["migration_history"][-1]["edge_hash"]
            body = {
                "from_factory_sha": old_factory,
                "from_head_sha": old_head,
                "to_factory_sha": target_factory,
                "to_head_sha": target_head,
            }
            edge_hash = digest({
                "body": body, "parent_hash": parent, "revision": number,
            })
            passport["migration_history"].append({
                **body, "edge_hash": edge_hash, "parent_hash": parent,
                "revision": number,
            })
            passport.update(
                factory_sha=target_factory,
                head_sha=target_head,
                parent_digest=edge_hash,
                tree_sha=tree_sha or passport["tree_sha"],
            )
            state["claim"]["head_sha"] = target_head
            if target_factory != old_factory:
                passport["factory_release_history"].append(target_factory)
                state["activation"]["active_factory_sha"] = target_factory
                state["activation"]["history"].append(target_factory)
            new_receipt(label)
            seal(state)
            validate(state)

        def append_success(role: str, label: str) -> None:
            passport = state["passport"]
            run_id = f"replay-{label}"
            charge = {
                "amount": 10_000_000,
                "factory_sha": passport["factory_sha"],
                "receipt": state["receipt"]["digest"],
                "role": role,
                "run_id": run_id,
            }
            passport["charges"].append(charge)
            passport["completed"].append({
                **charge, "output": digest(["output", run_id]),
            })
            passport["cumulative_charges_micro_usd"] += charge["amount"]

        def append_failure(role: str, label: str) -> None:
            passport = state["passport"]
            passport["charges"].append({
                "amount": 10_000_000,
                "factory_sha": passport["factory_sha"],
                "receipt": state["receipt"]["digest"],
                "role": role,
                "run_id": f"replay-{label}",
            })
            passport["cumulative_charges_micro_usd"] += 10_000_000

        seal(state)
        validate(state)
        self.assertEqual(len(state["passport"]["charges"]), 13)
        self.assertEqual(len(state["passport"]["completed"]), 6)
        self.assertEqual(
            state["passport"]["cumulative_charges_micro_usd"], 130_000_000
        )
        self.assertEqual(len(state["route"]["revisions"]), 27)
        self.assertEqual(len(state["passport"]["migration_history"]), 31)
        self.assertEqual(len(state["passport"]["factory_release_history"]), 24)

        trace = ["historical-passport-validated", "builder-blocked-and-parked"]
        provider_calls = []
        settled = {primary["ticket"]}
        launch_voids = {
            f"launch-void-{number:02d}"
            for number in range(primary["launch_void_count"])
        }
        terminalized = set()
        terminalized.update(launch_voids)
        terminalized.update(launch_voids)
        self.assertEqual(len(terminalized), 41)
        self.assertFalse(CONTROL.Controller.consumes_capacity({
            "lease": lease, "parked": True, "status": "blocked",
        }))
        self.assertIn(primary["ticket"], settled)
        trace.extend(["launch-void-terminalized-once", "invocation-settled"])

        sibling = fixture["tickets"]["independent_sibling"]
        provider_calls.append((sibling, "builder"))
        state["tickets"][sibling]["provider"] = False
        state["tickets"][sibling]["status"] = "validating"
        state["tickets"][sibling]["charges"].append("sibling-builder-run")
        state["tickets"][sibling]["completed"].append("sibling-builder-run")
        sibling_snapshot = copy.deepcopy(state["tickets"][sibling])
        trace.append("independent-sibling-progressed")

        accepted_resumes = set()
        resume_key = (state["repair"]["owner"], state["repair"]["blocked_receipt"])
        accepted_resumes.add(resume_key)
        self.assertIn(resume_key, accepted_resumes)
        self.assertFalse(resume_key not in accepted_resumes)
        trace.append("exact-resume-one-use")
        new_receipt("repair-a")
        provider_calls.append((primary["ticket"], "test-author"))
        append_success("test-author", "repair-a")
        state["repair"]["status"] = "completed"
        seal(state)
        validate(state)
        trace.append("repair-a-completed-and-archived")

        blocker_a = copy.deepcopy(state["repair"])
        planner_blocker = digest("planner-blocker")
        state["repair"] = {
            "blocked_receipt": planner_blocker,
            "directive_receipt": planner_blocker,
            "owner": "planner",
            "status": "pending",
        }
        self.assertNotEqual(blocker_a["blocked_receipt"], planner_blocker)
        self.assertTrue("FIX planner".startswith("FIX "))
        self.assertFalse("RUN planner".startswith("FIX "))
        new_receipt("planner-repair")
        provider_calls.append((primary["ticket"], "planner"))
        append_success("planner", "repair-planner")
        state["repair"]["status"] = "completed"
        seal(state)
        validate(state)
        trace.append("planner-repair-preflight-and-strict-output")

        current_blocker = digest("current-builder-blocker")
        state["repair"] = {
            "blocked_receipt": current_blocker,
            "directive_receipt": current_blocker,
            "owner": "test-author",
            "status": "pending",
        }
        passport_migration(
            "successor-a", factory_sha=successor_a, route_change=True
        )
        self.assertEqual(state["repair"]["owner"], "test-author")
        self.assertEqual(state["tickets"][sibling], sibling_snapshot)
        trace.extend(["successor-a-route-passport-migration", "controller-restart-no-replay"])

        old_identity = (state["claim"]["ticket"], state["claim"]["branch"])
        state["claim"]["cell"] = "/sealed/cells/rotated-primary"
        state["claim"]["lease"] = digest("rotated-lease")
        state["receipt"]["lease_sha256"] = hashlib.sha256(
            state["claim"]["lease"].encode()
        ).hexdigest()
        seal(state)
        validate(state)
        self.assertEqual(
            old_identity, (state["claim"]["ticket"], state["claim"]["branch"])
        )
        trace.append("cell-and-lease-rotated")

        state_machine_calls = 0
        paused_receipt = state["receipt"]["digest"]
        state_machine_calls += 1
        maintenance = True
        if maintenance:
            trace.append("stage-resolution-paused")
        self.assertEqual(state_machine_calls, 1)
        self.assertEqual(state["receipt"]["digest"], paused_receipt)
        trace.extend(["drain-refused-active-boundary", "stale-claim-refused"])

        cell = self.root / "composite-cell"
        (cell / "factory/tickets").mkdir(parents=True)
        (cell / "factory/route-plans").mkdir()
        (cell / "factory/runs").mkdir()
        (cell / "factory/tickets/T-710.md").write_text(
            "# T-710\n\nState: Building\nDepends-On: T-092\n",
            encoding="utf-8",
        )
        (cell / "factory/route-plans/T-710.json").write_text(
            '{"ticket":"T-710"}\n', encoding="utf-8"
        )

        def git(*arguments: str) -> str:
            return subprocess.run(
                ["git", "-C", str(cell), *arguments], text=True,
                capture_output=True, check=True,
            ).stdout.strip()

        git("init", "-q", "-b", "ticket/T-710")
        git("config", "user.name", "Composite Replay")
        git("config", "user.email", "replay@example.invalid")
        git("add", ".")
        git("commit", "-qm", "historical ticket checkpoint")
        git("checkout", "-qb", "main")
        (cell / "dependency.txt").write_text("T-092 merged\n", encoding="utf-8")
        git("add", "dependency.txt")
        git("commit", "-qm", "merge T-092")
        protected_head = git("rev-parse", "HEAD")
        git("update-ref", "refs/remotes/origin/main", protected_head)
        git("checkout", "-q", "ticket/T-710")

        state_args = argparse.Namespace(
            contract_version="1.8.0",
            factory_root=cell,
            factory_sha=successor_a,
            kit_dir=ROOT,
            lease=state["claim"]["lease"],
            project="replay",
            receipt="",
            require_used=False,
            role="",
            state_dir=self.state,
            ticket="T-710",
            workdir=cell,
        )
        controller = CONTROL.Controller(self.args)
        claim = {
            "branch": "ticket/T-710",
            "lease": state["claim"]["lease"],
            "parked": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": current_blocker,
            "role": "builder",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-710",
            "worktree": str(cell),
        }
        repairs = self.state / "contract-repairs"
        repairs.mkdir(mode=0o700, exist_ok=True)
        CONTROL.write(repairs / "T-710.json", {
            "blocked_receipt": current_blocker,
            "blocked_role": "builder",
            "repair_role": "test-author",
        })
        events = []
        transition_attempt_calls = []
        controller.remote_passport_valid = lambda _claim: True
        controller.ensure_lease = lambda _claim, _label: None
        controller.finish_pending_run = lambda _claim: True
        controller.refresh_dependency_tracking = lambda _claim: True
        controller.ticket_merged = lambda _claim: False
        controller.migrate_passport = lambda _claim, _state: None
        controller.event = lambda name, *_args, **_kwargs: events.append(name)

        def controller_json_call(*arguments, **_kwargs):
            if arguments[0] == "state-machine":
                transition_attempt_calls.append(arguments)
                return STATE.next_transition(state_args)
            if arguments[:2] == ("ticket-attest", "--ticket"):
                old_head = git("rev-parse", "HEAD")
                git("merge", "-q", "--no-edit", "origin/main")
                return {
                    "action": "dependency-refresh",
                    "attestation": {
                        "old_head": old_head,
                        "protected_head": protected_head,
                    },
                    "head": git("rev-parse", "HEAD"),
                }
            if arguments[:2] == ("publication", "withdraw"):
                return {"status": "absent"}
            raise AssertionError(f"unexpected helper call: {arguments}")

        controller.json_call = controller_json_call
        role_receipts = []

        def run_role(_claim, role, receipt, _failed_checks):
            state_args.receipt = receipt
            state_args.role = role
            STATE.verify(state_args, consume=True)
            role_receipts.append((role, receipt))

        controller.run_role = run_role
        with (
            patch.dict(
                os.environ,
                {"FACTORY_CERTIFIED_PRODUCT_ORIGIN": "sanitized-replay-origin"},
            ),
            patch.object(STATE, "protected_dependency", return_value={}),
            patch.object(STATE, "ensure_dependency_conflict_repair"),
            patch.object(
                STATE, "contract_repair_stage",
                return_value=("FIX test-author", True),
            ),
            patch.object(STATE, "migrate_passport"),
        ):
            calls_before_restore = len(transition_attempt_calls)
            self.assertTrue(controller.restore_recorded_contract_repair(claim))
            self.assertEqual(len(transition_attempt_calls), calls_before_restore)
            first = controller.reconcile_ticket(claim)
            self.assertEqual(first["status"], "progressed")
            self.assertEqual(len(transition_attempt_calls), 1)
            self.assertIn("dependency_base_refreshed", events)
            self.assertEqual(role_receipts, [])
            second = controller.reconcile_ticket(claim)
            self.assertEqual(second["status"], "progressed")
            self.assertEqual(len(transition_attempt_calls), 2)
            self.assertEqual(role_receipts[0][0], "test-author")
        trace.extend([
            "recorded-repair-prepared-without-resolution",
            "protected-dependency-refresh-provider-free",
            "repair-owner-resolved-next-attempt",
        ])

        refreshed_head = git("rev-parse", "HEAD")
        refreshed_tree = git("rev-parse", "HEAD^{tree}")
        state["passport"]["base_sha"] = protected_head
        state["receipt"]["base_sha"] = protected_head
        passport_migration(
            "dependency-refresh", head_sha=refreshed_head,
            tree_sha=refreshed_tree,
        )
        provider_calls.append((primary["ticket"], "test-author"))
        append_success("test-author", "repair-b")
        state["repair"]["status"] = "completed"
        seal(state)
        validate(state)
        trace.append("repair-b-completed-on-successor-a")

        new_receipt("reviewer-submission-unconfirmed")
        append_failure("reviewer", "reviewer-submission-unconfirmed")
        failed_submission_factory = state["passport"]["factory_sha"]
        failed_submission_receipt = state["receipt"]["digest"]
        seal(state)
        validate(state)
        self.assertEqual(failed_submission_factory, successor_a)
        self.assertEqual(
            sum(
                item["receipt"] == failed_submission_receipt
                for item in state["passport"]["charges"]
            ),
            1,
        )
        self.assertFalse(any(
            item["receipt"] == failed_submission_receipt
            for item in state["passport"]["completed"]
        ))
        trace.extend([
            "post-go-submission-unconfirmed-charged",
            "same-release-submission-replay-refused",
        ])

        passport_migration(
            "successor-b", factory_sha=successor_b, route_change=True
        )
        self.assertNotEqual(
            state["passport"]["factory_sha"], failed_submission_factory
        )
        self.assertEqual(state["tickets"][sibling], sibling_snapshot)
        trace.extend([
            "successor-b-route-passport-migration",
            "successor-release-submission-recovered",
            "second-restart-no-replay",
        ])

        state["publication"].update(queue=[primary["ticket"]], state="validating")
        trace.append("publication-returned-to-queue-tail")
        for role in ("reviewer", "narrator"):
            new_receipt(f"fresh-{role}")
            provider_calls.append((primary["ticket"], role))
            append_success(role, f"fresh-{role}")
        publication_head = state["passport"]["head_sha"]
        state["publication"].update(
            approval_head=publication_head,
            merge_lease=digest("short-product-merge-lease"),
            queue=[],
            reviewed_head=publication_head,
            state="merged",
            tested_head=publication_head,
        )
        state["passport"]["publication_state"] = "merged"
        state["tickets"][primary["ticket"]].update(
            provider=False, publication=False, status="done"
        )
        for ticket in fixture["tickets"]["dependents"]:
            state["tickets"][ticket]["status"] = "ready"
        seal(state)
        validate(state)
        trace.append("reviewer-narrator-publication-done")

        final_successes = len(state["passport"]["completed"])
        final_charges = len(state["passport"]["charges"])
        self.assertEqual(final_successes, 11)
        self.assertEqual(final_charges, 19)
        self.assertEqual(
            len([call for call in provider_calls if call[0] == primary["ticket"]]),
            5,
        )
        self.assertEqual(
            len([call for call in provider_calls if call[0] == sibling]), 1
        )
        self.assertEqual(state["tickets"][fixture["tickets"]["dormant"]]["status"], "backlog")
        self.assertEqual(len(state["route"]["revisions"]), 29)
        self.assertEqual(len(state["passport"]["migration_history"]), 34)
        self.assertEqual(len(state["passport"]["factory_release_history"]), 26)
        self.assertEqual(fixture_path.read_bytes(), fixture_raw)

        final = copy.deepcopy(state)
        sibling_final = copy.deepcopy(final["tickets"][sibling])
        providers_before_matrix = list(provider_calls)

        def resign(value: dict) -> None:
            value["claim"]["passport_sha256"] = digest(value["passport"])
            unsigned = {
                name: item for name, item in value.items()
                if name != "authentication_sha256"
            }
            value["authentication_sha256"] = hmac.new(
                key, STATE.canonical(unsigned), hashlib.sha256
            ).hexdigest()

        def tamper(value: dict, field: str) -> None:
            if field == "hmac":
                value["authentication_sha256"] = "0" * 64
                return
            if field == "passport_parent":
                value["passport"]["parent_digest"] = "0" * 64
            elif field == "migration_edge":
                value["passport"]["migration_history"][-1]["to_factory_sha"] = source
            elif field == "migration_gap":
                value["passport"]["migration_history"][-1]["revision"] += 1
            elif field == "head":
                value["claim"]["head_sha"] = oid("tampered-head")
            elif field == "tree":
                value["receipt"]["tree_sha"] = oid("tampered-tree")
            elif field == "route":
                value["passport"]["route_revision_hash"] = "0" * 64
            elif field == "base":
                value["receipt"]["base_sha"] = oid("tampered-base")
            elif field == "receipt":
                value["claim"]["transition_receipt"] = "0" * 64
            elif field == "charge_accounting":
                value["passport"]["cumulative_charges_micro_usd"] += 1
            elif field == "repair_directive_receipt":
                value["repair"]["directive_receipt"] = "0" * 64
            elif field == "conflict_blob":
                value["conflict"]["blob"] = "0" * 64
            elif field == "conflict_path":
                value["conflict"]["path"] = "apps/api/src/app.ts"
            elif field == "conflict_mode":
                value["conflict"]["mode"] = "120000"
            elif field == "lease":
                value["receipt"]["lease_sha256"] = "0" * 64
            elif field == "approval_head":
                value["publication"]["approval_head"] = oid("unreviewed-head")
            elif field == "publication_queue":
                value["publication"]["queue"] = [sibling]
            else:
                raise AssertionError(field)
            resign(value)

        self.assertEqual(set(fixture["tamper_fields"]), {
            "hmac", "passport_parent", "migration_edge", "migration_gap",
            "head", "tree", "route", "base", "receipt",
            "charge_accounting", "repair_directive_receipt", "conflict_blob",
            "conflict_path", "conflict_mode", "lease", "approval_head",
            "publication_queue",
        })
        for field in fixture["tamper_fields"]:
            altered = copy.deepcopy(final)
            tamper(altered, field)
            with self.subTest(tamper=field):
                with self.assertRaises(ValueError):
                    validate(altered)
                self.assertEqual(altered["tickets"][sibling], sibling_final)
                self.assertEqual(provider_calls, providers_before_matrix)

        self.assertEqual(len(trace), len(set(trace)))
        self.assertIn("protected-dependency-refresh-provider-free", trace)
        self.assertIn("reviewer-narrator-publication-done", trace)

    def test_upgrade_reconstructs_cleared_contract_blocker_fields(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        old_factory = "c" * 40
        head = "b" * 40
        receipt = self.operator_transition(
            "T-110", "RUN builder", role="builder", consumed=True,
            factory_sha=old_factory,
        )
        cell = self.root / "cell-1"
        (cell / "factory/route-plans").mkdir(parents=True)
        (cell / "factory/tickets").mkdir()
        (cell / "factory/route-plans/T-110.json").write_text(
            json.dumps({
                "kit_sha": self.release.name,
                "ticket": "T-110",
            }) + "\n",
            encoding="utf-8",
        )
        (cell / "factory/tickets/T-110.md").write_text(
            f"# T-110\n\nState: Review\nKit-SHA: {self.release.name}\n",
            encoding="utf-8",
        )
        (cell / "factory/KIT_PIN").write_text(
            self.release.name + "\n", encoding="utf-8",
        )
        claim = {
            "branch": "ticket/T-110",
            "lease": "e" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        manifest = self.product / "factory/runs/migrated-block.meta"
        manifest.write_text(
            "run_id=migrated-block\n"
            "ticket=T-110\n"
            "role=builder\n"
            "phase=completed\n"
            "accounting_state=completed\n"
            "exit_status=12\n"
            "role_exit=role_exit_contract_blocked\n"
            f"kit_sha={old_factory}\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )
        (self.state / "passports").mkdir(mode=0o700)
        CONTROL.write(
            self.state / "passports/T-110.json",
            {
                "branch": claim["branch"],
                "charge_records": [{
                    "contract_version": "1.8.0",
                    "factory_sha": old_factory,
                    "head_before": head,
                    "manifest_sha256": CONTROL.hashlib.sha256(
                        manifest.read_bytes()
                    ).hexdigest(),
                    "role": "builder",
                    "run_id": "migrated-block",
                    "transition_receipt_sha256": receipt,
                }],
                "completed_role_evidence": [],
                "factory_sha": old_factory,
                "ticket": "T-110",
            },
        )
        calls = []
        renew_failures = 1

        def json_call(*args, **_kwargs):
            nonlocal renew_failures
            calls.append(args)
            if args[0] == "renew" and renew_failures:
                renew_failures -= 1
                raise CONTROL.ControllerError("old lease was withdrawn")
            if args[0] == "claim":
                return {
                    "lease_id": "f" * 64,
                    "schema_version": 1,
                    "ticket": "T-110",
                }
            if args[:2] == ("passport", "migrate"):
                passport = CONTROL.read(
                    self.state / "passports/T-110.json"
                )
                passport["factory_sha"] = self.release.name
                CONTROL.write(
                    self.state / "passports/T-110.json", passport
                )
            return {}

        controller.json_call = json_call
        controller.remote_passport_valid = lambda _claim: True
        controller.event = lambda name, *_args, **_kwargs: calls.append((name,))

        transition_path = self.state / "T-110.json"
        transition = CONTROL.read(transition_path)
        altered = dict(transition)
        altered["stage"] = "FIX builder"
        CONTROL.write(transition_path, altered)
        passport_path = self.state / "passports/T-110.json"
        passport = CONTROL.read(passport_path)
        passport["factory_sha"] = self.release.name
        CONTROL.write(passport_path, passport)
        self.assertFalse(controller.restore_contract_blocker(claim))
        self.assertEqual(claim["lease"], "e" * 64)
        self.assertNotIn(("contract_blocker_claim_restored",), calls)
        self.assertFalse(any(call[0] == "claim" for call in calls))

        CONTROL.write(transition_path, transition)
        passport["factory_sha"] = old_factory
        CONTROL.write(passport_path, passport)
        calls.clear()
        controller.recover_upgraded_claims([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(claim["receipt"], receipt)
        self.assertEqual(claim["role"], "builder")
        self.assertEqual(claim["lease"], "f" * 64)
        self.assertIn(("contract_blocker_claim_restored",), calls)
        self.assertIn(("upgraded_claim_recovered",), calls)

    def test_exported_terminal_migrates_without_reexport(self) -> None:
        controller = CONTROL.Controller(self.args)
        receipt = "b" * 64
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": receipt,
            "role": "reviewer",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "running",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        (self.product / "factory/runs/exported.meta").write_text(
            "run_id=exported\n"
            "ticket=T-110\n"
            "role=reviewer\n"
            "accounting_state=completed\n"
            "exit_status=0\n"
            "role_exit=ok\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )
        (self.state / "passports").mkdir(mode=0o700)
        record = {
            "role": "reviewer",
            "run_id": "exported",
            "transition_receipt_sha256": receipt,
        }
        CONTROL.write(
            self.state / "passports/T-110.json",
            {
                "charge_records": [record],
                "completed_role_evidence": [record],
                "transition_receipt_sha256": receipt,
            },
        )
        calls = []
        controller.passport = lambda *_args: calls.append("export")
        controller.migrate_passport = lambda *_args: calls.append("migrate")
        controller.event = lambda name, *_args, **_kwargs: calls.append(name)
        controller.relocate_qualification_cell = lambda *_args: None
        controller.json_call = lambda *args, **_kwargs: (
            calls.append(args[0]) or {}
        )

        self.assertTrue(controller.finish_pending_run(claim))
        self.assertEqual(
            calls,
            [
                "attempt_terminal", "migrate", "terminal_export_recovered",
                "ticket-state", "migrate",
            ],
        )
        self.assertEqual(claim["status"], "claimed")
        self.assertNotIn("export", calls)

    def test_cancelled_run_releases_every_controller_resource(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {"tickets": ["T-110"]}
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "b" * 64,
            "receipt": "c" * 64,
            "role": "builder",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "running",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        (self.product / "factory/runs/cancelled.meta").write_text(
            "run_id=cancelled\n"
            "ticket=T-110\n"
            "role=builder\n"
            "accounting_state=cancelled_conservative\n"
            "exit_status=130\n"
            "role_exit=cancelled\n"
            f"transition_receipt_sha256={'c' * 64}\n",
            encoding="utf-8",
        )
        calls = []
        controller.passport = lambda *_args: calls.append("passport")
        controller.release = lambda *_args: calls.append("leases-and-claim")
        controller.remove_cell = lambda *_args: calls.append("cell")
        controller.event = lambda name, *_args, **_kwargs: calls.append(name)
        self.assertFalse(controller.finish_pending_run(claim))
        self.assertEqual(claim["status"], "cancelled")
        self.assertEqual(
            calls,
            [
                "attempt_terminal", "passport", "leases-and-claim", "cell",
                "attempt_cancelled",
            ],
        )
        self.assertTrue(controller.qualification_cohort_error.is_set())

    def test_complete_releases_claim(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        controller.finish_pending_run = lambda _claim: True

        def json_call(*args, **_kwargs):
            if args[0] == "state-machine":
                return state_transition(
                    "COMPLETE attested Done is on protected main"
                )
            if args[:2] == ("publication", "withdraw"):
                return {"status": "absent"}
            return {}

        controller.json_call = json_call
        self.assertEqual(
            controller.reconcile_ticket(claim),
            {"status": "complete", "ticket": "T-110"},
        )
        self.assertFalse(controller.claim_path("T-110").exists())

    def test_factory_upgrade_authenticates_passport_before_route_migration(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        (cell / "factory/route-plans").mkdir(parents=True)
        (cell / "factory/tickets").mkdir()
        route = cell / "factory/route-plans/T-110.json"
        ticket = cell / "factory/tickets/T-110.md"
        route.write_text(
            json.dumps({"kit_sha": "b" * 40, "ticket": "T-110"}) + "\n",
            encoding="utf-8",
        )
        ticket.write_text(
            f"# T-110\n\nState: Review\nKit-SHA: {'b' * 40}\n",
            encoding="utf-8",
        )
        pin = cell / "factory/KIT_PIN"
        pin.write_text("b" * 40 + "\n", encoding="utf-8")
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "lease_released": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "blocked_reason": "state-machine-escalation",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        (self.state / "passports").mkdir(mode=0o700)
        CONTROL.write(
            self.state / "passports/T-110.json",
            {"factory_sha": "b" * 40},
        )
        calls = []
        failures = 1

        def json_call(*args, **_kwargs):
            nonlocal failures
            calls.append(args)
            if args[0] == "passport":
                if failures:
                    failures -= 1
                    raise CONTROL.ControllerError("interrupted passport migration")
                CONTROL.write(
                    self.state / "passports/T-110.json",
                    {"factory_sha": "a" * 40},
                )
            if args[0] == "renew":
                raise CONTROL.ControllerError("old lease was released")
            if args[0] == "claim":
                return {
                    "lease_id": "c" * 64,
                    "schema_version": 1,
                    "ticket": "T-110",
                }
            return {}

        controller.json_call = json_call
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "interrupted passport migration"
        ):
            controller.recover_upgraded_claims([claim])
        self.assertTrue(controller.marker(
            "passport-route-migration-pending-T-110-" + "a" * 40
        ))
        controller.recover_upgraded_claims([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual([call[0] for call in calls], ["passport", "passport"])
        route.write_text(
            json.dumps({"kit_sha": "a" * 40, "ticket": "T-110"}) + "\n",
            encoding="utf-8",
        )
        ticket.write_text(
            f"# T-110\n\nState: Review\nKit-SHA: {'a' * 40}\n",
            encoding="utf-8",
        )
        pin.write_text("a" * 40 + "\n", encoding="utf-8")
        controller.recover_upgraded_claims([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertNotIn("blocked_reason", claim)
        self.assertEqual(claim["lease"], "c" * 64)
        self.assertNotIn("lease_released", claim)
        self.assertEqual(
            [call[0] for call in calls],
            ["passport", "passport", "renew", "claim", "passport"],
        )
        calls.clear()
        controller.renew = lambda _claim: calls.append(("renew-existing",))
        controller.ensure_lease(claim, "reconciliation")
        self.assertEqual(calls, [("renew-existing",)])

    def test_release_upgrade_recovers_merged_ticket_without_route_migration(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "parked/T-110"
        self.initialize_parked_branch(cell, "ticket/T-110")
        claim = {
            "branch": "ticket/T-110", "lease": "", "parked": True,
            "priority": "normal", "publication_lease": "", "receipt": "",
            "role": "", "schema": CONTROL.CLAIM_SCHEMA, "status": "blocked",
            "blocked_reason": "route-migration-required", "ticket": "T-110",
            "worktree": str(cell),
        }
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        passport = {
            "current_state": "Approved",
            "factory_sha": self.release.name,
            "passport_sha256": "b" * 64,
            "publication_state": "merged",
        }
        CONTROL.write(passports / "T-110.json", passport)
        controller.marker(
            f"passport-route-migration-pending-T-110-{self.release.name}",
            {
                "factory_sha": self.release.name,
                "schema": CONTROL.EVENT_SCHEMA,
                "ticket": "T-110",
            },
        )
        calls = []

        def json_call(*args, **_kwargs):
            calls.append(args)
            if args[0] == "renew":
                raise CONTROL.ControllerError("old lease is absent")
            if args[0] == "claim":
                return {
                    "lease_id": "c" * 64,
                    "schema_version": 1,
                    "ticket": "T-110",
                }
            if args[:2] == ("passport", "validate"):
                return {"passport": "b" * 64, "status": "ok"}
            return {}

        controller.json_call = json_call
        controller.ticket_merged = lambda _claim: True
        controller.event = lambda name, *_args, **_kwargs: calls.append((name,))
        controller.recover_upgraded_claims([claim])

        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["lease"], "c" * 64)
        self.assertNotIn("blocked_reason", claim)
        self.assertIn(("upgraded_merged_claim_recovered",), calls)
        self.assertTrue(controller.marker(
            f"passport-route-migration-complete-T-110-{self.release.name}"
        ))

    def test_completed_route_migration_clears_block_without_replaying_migration(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "parked/T-110"
        route = cell / "factory/route-plans/T-110.json"
        ticket = cell / "factory/tickets/T-110.md"
        route.parent.mkdir(parents=True)
        ticket.parent.mkdir(parents=True)
        route.write_text(json.dumps({
            "kit_sha": self.release.name, "ticket": "T-110",
        }) + "\n", encoding="utf-8")
        ticket.write_text(
            f"# T-110\n\nState: Review\nKit-SHA: {self.release.name}\n",
            encoding="utf-8",
        )
        (cell / "factory/KIT_PIN").write_text(
            self.release.name + "\n", encoding="utf-8",
        )
        self.initialize_parked_branch(cell, "ticket/T-110")
        claim = {
            "blocked_reason": "route-migration-required",
            "branch": "ticket/T-110", "lease": "a" * 64,
            "parked": True, "publication_lease": "", "receipt": "",
            "role": "", "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked", "ticket": "T-110", "worktree": str(cell),
        }
        (self.state / "passports").mkdir(mode=0o700)
        CONTROL.write(self.state / "passports/T-110.json", {
            "factory_sha": self.release.name,
        })
        for prefix in (
            "passport-route-migration-pending",
            "passport-route-migration-complete",
        ):
            controller.marker(
                f"{prefix}-T-110-{self.release.name}",
                {"factory_sha": self.release.name,
                 "schema": CONTROL.EVENT_SCHEMA, "ticket": "T-110"},
            )
        calls = []
        controller.renew = lambda _claim: calls.append("renew")
        controller.migrate_passport = lambda *_args: calls.append("migrate")
        controller.restore_contract_blocker = lambda _claim: False
        controller.event = lambda name, *_args, **_kwargs: calls.append(name)
        controller.event_once = lambda name, *_args, **_kwargs: calls.append(name)

        controller.recover_upgraded_claims([claim])
        controller.recover_upgraded_claims([claim])

        self.assertEqual(claim["status"], "claimed")
        self.assertNotIn("blocked_reason", claim)
        self.assertEqual(calls.count("renew"), 1)
        self.assertNotIn("migrate", calls)
        self.assertEqual(calls.count("route_migration_cleared"), 1)

    def test_release_bundle_refresh_requires_stale_protected_base(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "parked/T-110"
        bundle = cell / "factory/attestations/T-110/bundle.json"
        bundle.parent.mkdir(parents=True)
        bundle.write_text(json.dumps({"kit_sha": "b" * 40}), encoding="utf-8")
        claim = {
            "branch": "ticket/T-110", "lease": "c" * 64,
            "priority": "normal", "publication_lease": "", "receipt": "",
            "role": "", "schema": CONTROL.CLAIM_SCHEMA, "status": "blocked",
            "ticket": "T-110", "worktree": str(cell),
        }
        passport = {
            "current_state": "Awaiting Approval",
            "factory_release_history": [
                {"factory_sha": "b" * 40},
                {"factory_sha": self.release.name},
            ],
            "factory_sha": self.release.name,
            "head_sha": "d" * 40,
            "publication_state": "validating",
        }
        controller.remote_passport_valid = lambda _claim: True
        controller.protected_base_current = lambda *_args: True
        self.assertFalse(controller.release_bundle_refreshable(claim, passport))
        controller.protected_base_current = lambda *_args: False
        controller.ticket_release_current = lambda _claim: False
        self.assertFalse(controller.release_bundle_refreshable(claim, passport))
        controller.ticket_release_current = lambda _claim: True
        self.assertTrue(controller.release_bundle_refreshable(claim, passport))
        passport["publication_state"] = "merged"
        self.assertFalse(controller.release_bundle_refreshable(claim, passport))

    def test_release_upgrade_reclaims_preserved_bundle_refresh(self) -> None:
        ticket_id = "T-110"
        source = "f" * 40
        target = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        product = self.root / "migration-product"
        remote = self.root / "migration-product.git"
        cell = self.root / "parked/T-110"
        (product / "factory/tickets").mkdir(parents=True)
        (product / "factory/KIT_PIN").write_text(target + "\n")
        (product / "factory/PROJECT.env").write_text(
            "GH_REPO=nysa-company/migration-product\n"
            "TICKET_BRANCH_PREFIX=ticket/\n"
        )
        (product / "factory/tickets/T-110.md").write_text(
            "# T-110\n\nState: Review\n", encoding="utf-8",
        )
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(product)], check=True,
        )
        for name, value in (
            ("user.name", "Software Factory"),
            ("user.email", "factory@local"),
        ):
            subprocess.run(
                ["git", "-C", str(product), "config", name, value], check=True,
            )
        subprocess.run(["git", "-C", str(product), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(product), "commit", "-qm", "baseline"],
            check=True,
        )
        base = subprocess.run(
            ["git", "-C", str(product), "rev-parse", "HEAD"], text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        subprocess.run(
            ["git", "-C", str(product), "remote", "add", "origin", str(remote)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(product), "push", "-qu", "origin", "main"],
            check=True,
        )
        subprocess.run(
            [
                "git", "-C", str(product), "worktree", "add", "-q", "-b",
                "ticket/T-110", str(cell),
            ],
            check=True,
        )
        ticket = cell / "factory/tickets/T-110.md"
        route = cell / "factory/route-plans/T-110.json"
        bundle = cell / "factory/attestations/T-110/bundle.json"
        route.parent.mkdir(parents=True)
        bundle.parent.mkdir(parents=True)
        catalog, routes, _profiles, profile_map = ROUTER.load_policy()
        readiness = {
            route_id: {
                "adapter_version": "test-v1", "reason": "ok",
                "reported_identity": value["expected_reported_identity"],
                "state": "READY",
            }
            for route_id, value in routes.items() if value["enabled"]
        }
        resolution = ROUTER.resolve_policy(
            catalog, routes, profile_map["cursor-opus-v1"], readiness,
        )
        ticket.write_text(
            f"# T-110\n\nState: Review\nKit-SHA: {source}\n",
            encoding="utf-8",
        )
        route.write_text(ROUTER.canonical_json({
            "created_at": "2026-08-09T00:00:00Z", "kit_sha": source,
            "resolution": resolution, "schema": "ticket-model-route-plan/v1",
            "ticket": ticket_id,
        }) + "\n", encoding="utf-8")
        bundle.write_text(
            json.dumps({"kit_sha": source}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(cell), "add", "factory"], check=True,
        )
        subprocess.run(
            ["git", "-C", str(cell), "commit", "-qm", "prior-kit bundle"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(cell), "push", "-qu", "origin", "HEAD"],
            check=True,
        )
        source_head = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD"], text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        source_tree = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD^{tree}"], text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        source_route = hashlib.sha256(route.read_bytes()).hexdigest()
        source_ticket_blob = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD:factory/tickets/T-110.md"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()

        authorization = (
            product / "factory/migrations/inflight-release" / f"{target}.json"
        )
        authorization.parent.mkdir(parents=True)
        authorization.write_text(CONTROL.canonical({
            "repository": "nysa-company/migration-product",
            "schema": "nysa.software-factory.inflight-release-authorization/v1",
            "source_kit_sha": source, "target_kit_sha": target,
            "tickets": [{
                "branch": "ticket/T-110", "head": source_head,
                "state": "Review", "ticket": ticket_id,
            }],
        }) + "\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(product), "add", str(authorization)], check=True,
        )
        subprocess.run(
            ["git", "-C", str(product), "commit", "-qm", "authorize migration"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(product), "push", "-q", "origin", "main"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(cell), "fetch", "-q", "origin", "main"],
            check=True,
        )

        release = self.root / target
        shutil.copytree(ROOT / "scripts", release / "scripts")
        contract = release / "factory-contract.json"
        shutil.copy2(ROOT / "factory-contract.json", contract)
        release_tree = subprocess.run(
            [
                "bash", "-c", 'source "$1"; factory_directory_tree "$2"',
                "_", str(ROOT / "scripts/lib/kit-pin.sh"), str(release),
            ],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        model_state = self.root / "model-state"
        model_state.mkdir()
        global_env = self.root / "global.env"
        global_env.write_text("\n".join((
            "CODEX_PINNED=0.144.1",
            "CLAUDE_CODE_PINNED=2.1.207",
            "FACTORY_CURSOR_FALLBACK_ENABLED=1",
            "CURSOR_AGENT_VERSION=2026.07.23-e383d2b",
            "CURSOR_OPENAI_MODEL=gpt-5.6-sol-high",
            "CURSOR_ANTHROPIC_MODEL=claude-sonnet-5-thinking-high",
            "FACTORY_PROBE_CODEX=READY:test",
            "FACTORY_PROBE_CLAUDE_CODE=READY:test",
            "FACTORY_PROBE_CURSOR_OPENAI=READY:test",
            "FACTORY_PROBE_CURSOR_ANTHROPIC=READY:test", "",
        )))
        environment = {
            **os.environ,
            "FACTORY_CERTIFIED_PRODUCT_ORIGIN": str(remote),
            "FACTORY_GLOBAL_ENV": str(global_env),
            "FACTORY_MODEL_STATE_ROOT": str(model_state),
            "FACTORY_PROJECT": "relay", "FACTORY_ROOT": str(product),
            "FACTORY_RELEASE_CONTRACT_VERSION": "2.0.0",
            "FACTORY_RELEASE_PATH": str(release),
            "FACTORY_RELEASE_SHA": target,
            "FACTORY_RELEASE_TREE": release_tree,
            "FACTORY_TEST_MODE": "1", "FACTORY_TRUSTED_TEST_HARNESS": "1",
        }

        def models(*arguments):
            result = subprocess.run(
                [str(release / "scripts/model-control.sh"), *arguments],
                env=environment, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            return json.loads(result.stdout)

        preview = models(
            "migrate-plan", "--ticket", ticket_id, "--workdir", str(cell),
        )
        applied = models(
            "migrate", "--ticket", ticket_id, "--workdir", str(cell),
            "--approve-hash", preview["preview_hash"], "--readiness-hash",
            preview["readiness_sha256"], "--approved-by", "release-upgrade",
        )
        migrated_head = applied["commit_sha"]
        migrated_route = hashlib.sha256(route.read_bytes()).hexdigest()
        self.assertNotEqual(migrated_head, source_head)
        self.assertEqual(
            sorted(subprocess.run(
                [
                    "git", "-C", str(cell), "diff-tree", "--no-commit-id",
                    "--name-only", "-r", migrated_head,
                ],
                text=True, capture_output=True, check=True,
            ).stdout.splitlines()),
            [
                "factory/route-plans/T-110.json",
                "factory/tickets/T-110.md",
            ],
        )
        self.assertEqual(bundle.read_text(), json.dumps(
            {"kit_sha": source}, sort_keys=True,
        ) + "\n")
        replay_preview = models(
            "migrate-plan", "--ticket", ticket_id, "--workdir", str(cell),
        )
        replay = models(
            "migrate", "--ticket", ticket_id, "--workdir", str(cell),
            "--approve-hash", replay_preview["preview_hash"],
            "--readiness-hash", replay_preview["readiness_sha256"],
            "--approved-by", "release-upgrade",
        )
        self.assertTrue(replay["recovered"])
        self.assertEqual(replay["commit_sha"], migrated_head)
        bundle.write_text(
            json.dumps({"kit_sha": "e" * 40}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(cell), "add", str(bundle)], check=True,
        )
        subprocess.run(
            ["git", "-C", str(cell), "commit", "-qm", "incoherent bundle"],
            check=True,
        )
        incoherent_head = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD"], text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        incoherent = subprocess.run(
            [
                str(release / "scripts/model-control.sh"), "migrate-plan",
                "--ticket", ticket_id, "--workdir", str(cell),
            ],
            env=environment, text=True, capture_output=True,
        )
        self.assertEqual(incoherent.returncode, 2)
        self.assertIn(
            "bundle attestation must be invalidated before route migration",
            incoherent.stdout,
        )
        self.assertEqual(subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD"], text=True,
            capture_output=True, check=True,
        ).stdout.strip(), incoherent_head)
        subprocess.run(
            ["git", "-C", str(cell), "reset", "--hard", "-q", migrated_head],
            check=True,
        )

        key = self.state / "passport.key"
        key.write_bytes(b"k" * 32)
        key.chmod(0o600)
        passport_path = self.state / "passports/T-110.json"
        passport_path.parent.mkdir(mode=0o700)
        old_passport = PASSPORT.authenticate({
            "base_history": [source_head], "branch": "ticket/T-110",
            "charge_records": [], "completed_role_evidence": [],
            "contract_version": "1.8.0", "current_state": "Awaiting Approval",
            "factory_release_history": [{
                "contract_version": "1.8.0", "factory_sha": source,
            }],
            "factory_sha": source, "head_sha": source_head,
            "head_tree": source_tree, "migration_history": [],
            "product_origin_sha256": hashlib.sha256(
                str(remote).encode()
            ).hexdigest(),
            "project": "relay", "protected_base_sha": base,
            "publication_state": "validating",
            "route_plan_sha256": source_route,
            "schema": "nysa.software-factory.ticket-passport/v1",
            "ticket": ticket_id, "ticket_blob": source_ticket_blob,
            "transition_receipt_sha256": "",
        }, key.read_bytes())
        PASSPORT.write_atomic(passport_path, old_passport)
        old_file = hashlib.sha256(passport_path.read_bytes()).hexdigest()
        release_edge = {
            "from_factory_sha": source, "from_head_sha": source_head,
            "from_passport_file_sha256": old_file,
            "from_passport_sha256": old_passport["passport_sha256"],
            "from_protected_base_sha": base,
            "from_route_plan_sha256": source_route,
            "schema": PASSPORT.MIGRATION_SCHEMA,
            "to_factory_sha": target, "to_head_sha": source_head,
            "to_protected_base_sha": base,
            "to_route_plan_sha256": source_route,
        }
        before_route = PASSPORT.authenticate({
            **{
                name: value for name, value in old_passport.items()
                if name not in {
                    "authentication_sha256", "passport_sha256",
                }
            },
            "factory_release_history": [
                *old_passport["factory_release_history"],
                {"contract_version": "1.8.0", "factory_sha": target},
            ],
            "factory_sha": target, "migration_history": [release_edge],
            "parent_digest": old_passport["passport_sha256"],
            "parent_file_sha256": old_file,
        }, key.read_bytes())
        PASSPORT.write_atomic(passport_path, before_route)
        stale = self.stale_release_receipt(
            ticket_id, source, "a" * 64, head=source_head,
            route=source_route, passport_file=old_file,
        )
        claim = {
            "branch": "ticket/T-110", "lease": "a" * 64,
            "priority": "normal", "publication_lease": "", "receipt": "",
            "release_refresh_required": True, "role": "",
            "schema": CONTROL.CLAIM_SCHEMA, "status": "blocked",
            "blocked_reason": "route-migration-required", "ticket": "T-110",
            "worktree": str(cell),
        }
        args = argparse.Namespace(
            launcher=self.launcher, product_root=product, project="relay",
            release_path=release, state_dir=self.state,
        )
        controller = CONTROL.Controller(args)
        controller.marker(
            f"passport-route-migration-pending-T-110-{target}",
            {
                "factory_sha": target,
                "schema": CONTROL.EVENT_SCHEMA,
                "ticket": "T-110",
            },
        )
        controller.save_claim(claim)
        passport_calls = []

        def migrate_passport(*_args):
            passport_calls.append("passport")
            before = controller.authenticated_operator_passport(ticket_id)
            parent_file = hashlib.sha256(passport_path.read_bytes()).hexdigest()
            edge = {
                "from_factory_sha": target, "from_head_sha": source_head,
                "from_passport_file_sha256": parent_file,
                "from_passport_sha256": before["passport_sha256"],
                "from_protected_base_sha": base,
                "from_route_plan_sha256": source_route,
                "schema": PASSPORT.MIGRATION_SCHEMA,
                "to_factory_sha": target, "to_head_sha": migrated_head,
                "to_protected_base_sha": base,
                "to_route_plan_sha256": migrated_route,
            }
            migrated = PASSPORT.authenticate({
                **{
                    name: value for name, value in before.items()
                    if name not in {
                        "authentication_sha256", "passport_sha256",
                        "parent_digest", "parent_file_sha256",
                    }
                },
                "head_sha": migrated_head,
                "head_tree": subprocess.run(
                    ["git", "-C", str(cell), "rev-parse", "HEAD^{tree}"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip(),
                "migration_history": [*before["migration_history"], edge],
                "parent_digest": before["passport_sha256"],
                "parent_file_sha256": parent_file,
                "route_plan_sha256": migrated_route,
                "ticket_blob": subprocess.run(
                    [
                        "git", "-C", str(cell), "rev-parse",
                        "HEAD:factory/tickets/T-110.md",
                    ],
                    text=True, capture_output=True, check=True,
                ).stdout.strip(),
            }, key.read_bytes())
            PASSPORT.write_atomic(passport_path, migrated)

        def state_machine(*_args, **_kwargs):
            current_file = hashlib.sha256(passport_path.read_bytes()).hexdigest()
            value = dict(stale)
            value.update(
                factory_sha=target, head_sha=migrated_head,
                parent_digest=stale["receipt_sha256"],
                passport_sha256=current_file,
                route_plan_sha256=migrated_route,
                stage="AWAIT-OPERATOR operator approval observed",
            )
            value.pop("receipt_sha256")
            value["receipt_sha256"] = hashlib.sha256(STATE.canonical({
                name: item for name, item in value.items()
                if name not in {
                    "consumed", "consumed_at_epoch", "receipt_sha256",
                }
            })).hexdigest()
            CONTROL.write(self.state / "T-110.json", value)
            return state_transition(
                value["stage"], value["receipt_sha256"], ticket_id,
            )

        controller.json_call = state_machine
        controller.migrate_passport = migrate_passport

        def locally_valid_operator_passport(_claim):
            current = controller.authenticated_operator_passport(ticket_id)
            head = subprocess.run(
                ["git", "-C", str(cell), "rev-parse", "HEAD"],
                text=True, capture_output=True, check=True,
            ).stdout.strip()
            if current["head_sha"] != head:
                raise CONTROL.ControllerError(
                    "passport does not match this clean execution cell"
                )
            return current

        controller.locally_valid_operator_passport = (
            locally_valid_operator_passport
        )
        controller.renew = lambda _claim: None
        controller.recover_upgraded_claims([claim])

        receipt_bytes = (self.state / "T-110.json").read_bytes()
        self.assertEqual(claim["status"], "claimed")
        self.assertTrue(claim["release_refresh_required"])
        self.assertFalse(controller.marker(
            f"passport-route-migration-complete-T-110-{target}"
        ))

        rotated_lease = "e" * 64
        claim.update(lease=rotated_lease, status="waiting")
        controller.save_claim(claim)
        restarted = CONTROL.Controller(args)
        restarted.locally_valid_operator_passport = (
            locally_valid_operator_passport
        )
        restarted.renew = lambda _claim: None
        restarted.json_call = lambda *_args, **_kwargs: self.fail(
            "rotated lease must reuse the completed receipt handoff"
        )
        restarted.migrate_passport = lambda *_args: passport_calls.append(
            "passport-rotated-lease-replay"
        )
        restarted.release_bundle_refreshable = lambda *_args: self.fail(
            "completed receipt handoff must not refresh again"
        )
        claim = restarted.load_claims()[0]
        restarted.recover_upgraded_claims([claim])

        self.assertEqual(
            passport_calls, ["passport", "passport-rotated-lease-replay"],
        )
        self.assertEqual((self.state / "T-110.json").read_bytes(), receipt_bytes)
        self.assertEqual(claim["status"], "claimed")
        self.assertNotIn("blocked_reason", claim)
        self.assertTrue(claim["release_refresh_required"])
        self.assertTrue(restarted.marker(
            f"passport-route-migration-complete-T-110-{target}"
        ))

        calls = []

        def reconcile_call(*arguments, **_kwargs):
            calls.append(arguments)
            if arguments[0] == "state-machine":
                return state_transition(
                    "AWAIT-OPERATOR operator approval observed; trusted "
                    "approval attestation is required",
                    "f" * 64,
                    ticket_id,
                )
            if arguments[0] == "ticket-attest":
                return {"action": "refresh", "head": "d" * 40}
            if arguments[:2] == ("publication", "withdraw"):
                return {"status": "absent"}
            return {}

        restarted.json_call = reconcile_call
        restarted.finish_pending_run = lambda _claim: True
        restarted.refresh_dependency_tracking = lambda _claim: True
        restarted.ticket_merged = lambda _claim: False
        restarted.renew = lambda _claim: None
        restarted.migrate_passport = lambda *_args: passport_calls.append(
            "passport-refresh"
        )
        self.assertEqual(
            restarted.reconcile_ticket(claim),
            {"status": "blocked", "ticket": ticket_id},
        )

        self.assertEqual(
            passport_calls,
            ["passport", "passport-rotated-lease-replay", "passport-refresh"],
        )
        self.assertEqual(
            sum(call[0] == "ticket-attest" for call in calls if call), 1,
        )
        self.assertEqual(claim["blocked_reason"], "route-migration-required")
        self.assertNotIn("release_refresh_required", claim)

    def migrated_bundle_passport(
        self, ticket: str, prior: str, head: str = "b" * 40,
        route: str = "e" * 64, intermediates: tuple[str, ...] = (),
    ) -> tuple[dict[str, object], str]:
        key_path = self.state / "passport.key"
        if not key_path.exists():
            key_path.write_bytes(b"k" * 32)
            key_path.chmod(0o600)
        releases = (prior, *intermediates, self.release.name)
        files = tuple(
            "7" * 64 if index == 0 else f"{7 + index:x}" * 64
            for index in range(len(releases) - 1)
        )
        digests = tuple(
            "6" * 64 if index == 0 else f"{8 + index:x}" * 64
            for index in range(len(releases) - 1)
        )
        protected = tuple(
            f"{index + 1:040x}" for index in range(len(releases))
        )
        migrations = [
            {
                "from_factory_sha": before, "from_head_sha": head,
                "from_passport_file_sha256": files[index],
                "from_passport_sha256": digests[index],
                "from_protected_base_sha": protected[index],
                "from_route_plan_sha256": route,
                "schema": CONTROL.PASSPORT_MIGRATION_SCHEMA,
                "to_factory_sha": after, "to_head_sha": head,
                "to_protected_base_sha": protected[index + 1],
                "to_route_plan_sha256": route,
            }
            for index, (before, after) in enumerate(zip(releases, releases[1:]))
        ]
        final_head = "c" * 40
        final_route = "d" * 64
        migrations.append({
            "from_factory_sha": self.release.name, "from_head_sha": head,
            "from_passport_file_sha256": "a" * 64,
            "from_passport_sha256": "b" * 64,
            "from_protected_base_sha": protected[-1],
            "from_route_plan_sha256": route,
            "schema": CONTROL.PASSPORT_MIGRATION_SCHEMA,
            "to_factory_sha": self.release.name, "to_head_sha": final_head,
            "to_protected_base_sha": protected[-1],
            "to_route_plan_sha256": final_route,
        })
        passport = PASSPORT.authenticate({
            "base_history": [head], "branch": f"ticket/{ticket}",
            "charge_records": [], "completed_role_evidence": [],
            "contract_version": "1.8.0", "current_state": "Awaiting Approval",
            "factory_release_history": [
                {"contract_version": "1.8.0", "factory_sha": release}
                for release in releases
            ],
            "factory_sha": self.release.name, "head_sha": final_head,
            "head_tree": "c" * 40,
            "migration_history": migrations,
            "parent_digest": "b" * 64,
            "parent_file_sha256": "a" * 64,
            "product_origin_sha256": "5" * 64, "project": "relay",
            "protected_base_sha": protected[-1],
            "publication_state": "validating",
            "route_plan_sha256": final_route,
            "schema": "nysa.software-factory.ticket-passport/v1",
            "ticket": ticket, "ticket_blob": "4" * 40,
            "transition_receipt_sha256": "",
        }, key_path.read_bytes())
        path = self.state / "passports" / f"{ticket}.json"
        path.parent.mkdir(mode=0o700, exist_ok=True)
        PASSPORT.write_atomic(path, passport)
        return passport, hashlib.sha256(path.read_bytes()).hexdigest()

    def stale_release_receipt(
        self, ticket: str, prior: str, lease: str, head: str = "b" * 40,
        route: str = "e" * 64, passport_file: str = "7" * 64,
        stage: str = "AWAIT-OPERATOR bundle attested; await operator approval",
    ) -> dict[str, object]:
        value = {
            "branch": f"ticket/{ticket}",
            "consumed": False,
            "contract_version": "1.8.0",
            "factory_sha": prior,
            "head_sha": head,
            "lease_sha256": hashlib.sha256(lease.encode()).hexdigest(),
            "parent_digest": "9" * 64,
            "passport_sha256": passport_file,
            "project": "relay",
            "role": None,
            "route_plan_sha256": route,
            "schema": "nysa.software-factory.transition-receipt/v1",
            "stage": stage,
            "ticket": ticket,
        }
        value["receipt_sha256"] = hashlib.sha256(STATE.canonical({
            key: item for key, item in value.items()
            if key not in {"consumed", "consumed_at_epoch", "receipt_sha256"}
        })).hexdigest()
        CONTROL.write(self.state / f"{ticket}.json", value)
        return value

    def test_bundle_refresh_receipt_handoff_survives_restart(self) -> None:
        prior = "f" * 40
        lease = "a" * 64
        controller = CONTROL.Controller(self.args)
        claims = {}
        for ticket in ("T-110", "T-111"):
            cell = self.root / f"parked/{ticket}"
            cell.mkdir(parents=True)
            self.migrated_bundle_passport(ticket, prior)
            controller.marker(
                f"passport-route-migration-pending-{ticket}-"
                f"{self.release.name}",
                {
                    "factory_sha": self.release.name,
                    "schema": CONTROL.EVENT_SCHEMA,
                    "ticket": ticket,
                },
            )
            claims[ticket] = {
                "blocked_reason": "route-migration-required",
                "branch": f"ticket/{ticket}", "lease": lease,
                "priority": "normal", "publication_lease": "", "receipt": "",
                "release_refresh_required": True, "role": "",
                "schema": CONTROL.CLAIM_SCHEMA, "status": "blocked",
                "ticket": ticket, "worktree": str(cell),
            }
            controller.save_claim(claims[ticket])
        stale = self.stale_release_receipt("T-110", prior, lease)
        sibling_stale = self.stale_release_receipt("T-111", prior, lease)
        passport_file = hashlib.sha256(
            (self.state / "passports/T-110.json").read_bytes()
        ).hexdigest()

        def state_machine(*args, **_kwargs):
            ticket = args[args.index("--ticket") + 1]
            value = dict(stale)
            value.update(
                factory_sha=self.release.name,
                head_sha="c" * 40,
                parent_digest=stale["receipt_sha256"],
                passport_sha256=passport_file,
                route_plan_sha256="d" * 64,
                stage="AWAIT-OPERATOR operator approval observed",
            )
            value.pop("receipt_sha256")
            value["receipt_sha256"] = hashlib.sha256(STATE.canonical({
                key: item for key, item in value.items()
                if key not in {
                    "consumed", "consumed_at_epoch", "receipt_sha256",
                }
            })).hexdigest()
            CONTROL.write(self.state / f"{ticket}.json", value)
            return state_transition(
                "AWAIT-OPERATOR operator approval observed",
                value["receipt_sha256"], ticket,
            )

        controller.json_call = state_machine
        controller.release_bundle_refreshable = (
            lambda claim, *_args: claim["ticket"] == "T-110"
        )
        controller.ticket_release_current = lambda _claim: False
        controller.renew = lambda _claim: None
        controller.role_active = lambda _claim: False
        original_event_once = controller.event_once

        def crash_after_receipt(name, *args, **kwargs):
            if name == "prior_release_receipt_refreshed":
                raise RuntimeError("crash after receipt")
            return original_event_once(name, *args, **kwargs)

        controller.event_once = crash_after_receipt
        with self.assertRaisesRegex(RuntimeError, "crash after receipt"):
            controller.recover_upgraded_claims([claims["T-110"]])

        receipt_path = self.state / "T-110.json"
        issued_bytes = receipt_path.read_bytes()
        self.assertEqual(CONTROL.read(receipt_path)["factory_sha"], self.release.name)
        self.assertEqual(
            CONTROL.read(self.state / "T-111.json")["receipt_sha256"],
            sibling_stale["receipt_sha256"],
        )

        restarted = CONTROL.Controller(self.args)
        restarted.release_bundle_refreshable = lambda *_args: True
        restarted.ticket_release_current = lambda _claim: False
        restarted.renew = lambda _claim: None
        restarted.role_active = lambda _claim: False
        restarted.json_call = lambda *_args, **_kwargs: self.fail(
            "restart must reuse the durable current receipt"
        )
        claim = restarted.load_claims()[0]
        restarted.recover_upgraded_claims([claim])

        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["receipt"], "")
        self.assertTrue(claim["release_refresh_required"])
        self.assertEqual(receipt_path.read_bytes(), issued_bytes)
        self.assertNotIn("T-110", restarted.prior_transition_tickets)

    def test_bundle_refresh_receipt_crosses_two_releases_and_rotated_lease(
        self,
    ) -> None:
        prior = "f" * 40
        intermediate = "d" * 40
        old_lease = "b" * 64
        current_lease = "c" * 64
        cell = self.root / "parked/T-110"
        cell.mkdir(parents=True)
        passport, _passport_file = self.migrated_bundle_passport(
            "T-110", prior, intermediates=(intermediate,),
        )
        passport = PASSPORT.authenticate({
            **{
                key: value for key, value in passport.items()
                if key not in {"authentication_sha256", "passport_sha256"}
            },
            "current_state": "Approved",
            "publication_state": "merge-pending",
        }, (self.state / "passport.key").read_bytes())
        PASSPORT.write_atomic(
            self.state / "passports/T-110.json", passport,
        )
        stale = self.stale_release_receipt(
            "T-110", prior, old_lease,
            stage="AWAIT-MERGE approval attested; protected auto-merge request pending",
        )
        stale["consumed"] = True
        stale["consumed_at_epoch"] = 1
        CONTROL.write(self.state / "T-110.json", stale)
        claim = {
            "blocked_reason": "route-migration-required",
            "branch": "ticket/T-110", "lease": current_lease,
            "priority": "normal", "publication_lease": "", "receipt": "",
            "release_refresh_required": True, "role": "",
            "schema": CONTROL.CLAIM_SCHEMA, "status": "blocked",
            "ticket": "T-110", "worktree": str(cell),
        }
        passport_file = hashlib.sha256(
            (self.state / "passports/T-110.json").read_bytes()
        ).hexdigest()
        marker = (
            self.state / f"bundle-refresh-transition-T-110-{self.release.name}.json"
        )
        controller = CONTROL.Controller(self.args)
        controller.role_active = lambda _claim: False
        controller.json_call = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("crash before receipt")
        )

        with self.assertRaisesRegex(RuntimeError, "crash before receipt"):
            controller.refresh_prior_release_receipt(claim)
        marker_bytes = marker.read_bytes()
        claim.pop("release_refresh_required")
        controller.release_ticket_lease = lambda _claim: self.fail(
            "exact handoff lease must survive restart"
        )
        controller.release_inactive_ticket_leases([claim])

        def state_machine(*args, **_kwargs):
            self.assertEqual(
                args[args.index("--lease") + 1], current_lease,
            )
            value = dict(stale)
            value.update(
                consumed=False,
                factory_sha=self.release.name,
                head_sha="c" * 40,
                lease_sha256=hashlib.sha256(current_lease.encode()).hexdigest(),
                parent_digest=stale["receipt_sha256"],
                passport_sha256=passport_file,
                route_plan_sha256="d" * 64,
                stage=(
                    "AWAIT-MERGE approval attested; protected auto-merge request "
                    "pending"
                ),
            )
            value.pop("consumed_at_epoch", None)
            value.pop("receipt_sha256")
            value["receipt_sha256"] = hashlib.sha256(STATE.canonical({
                key: item for key, item in value.items()
                if key not in {
                    "consumed", "consumed_at_epoch", "receipt_sha256",
                }
            })).hexdigest()
            CONTROL.write(self.state / "T-110.json", value)
            return state_transition(
                value["stage"], value["receipt_sha256"], "T-110",
            )

        restarted = CONTROL.Controller(self.args)
        restarted.role_active = lambda _claim: False
        restarted.json_call = state_machine
        refreshed = restarted.refresh_prior_release_receipt(claim)
        current = CONTROL.read(self.state / "T-110.json")

        self.assertEqual(current["receipt_sha256"], refreshed)
        self.assertEqual(current["stage"], stale["stage"])
        self.assertEqual(current["parent_digest"], stale["receipt_sha256"])
        self.assertEqual(
            current["lease_sha256"],
            hashlib.sha256(current_lease.encode()).hexdigest(),
        )
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(marker.read_bytes(), marker_bytes)

    def test_bundle_refresh_migration_suffix_is_exact(self) -> None:
        prior = "f" * 40
        intermediate = "d" * 40
        controller = CONTROL.Controller(self.args)
        claim = {"ticket": "T-110", "worktree": str(self.product)}
        passport, _file = self.migrated_bundle_passport(
            "T-110", prior, intermediates=(intermediate,),
        )
        edges = passport["migration_history"]
        edges[0]["to_head_sha"] = "a" * 40
        edges[0]["to_route_plan_sha256"] = "1" * 64
        edges[1]["from_head_sha"] = "a" * 40
        edges[1]["from_route_plan_sha256"] = "1" * 64
        edges[1]["to_head_sha"] = "9" * 40
        edges[1]["to_route_plan_sha256"] = "2" * 64
        edges[2]["from_head_sha"] = "9" * 40
        edges[2]["from_route_plan_sha256"] = "2" * 64
        self.assertEqual(
            len(controller.bundle_refresh_migration_suffix(
                claim, passport, prior, "7" * 64,
            )),
            3,
        )
        with_route = copy.deepcopy(passport)
        route_edge = copy.deepcopy(with_route["migration_history"][-1])
        route_edge.update(
            from_head_sha="9" * 40,
            from_passport_file_sha256="3" * 64,
            from_passport_sha256="4" * 64,
            from_route_plan_sha256="2" * 64,
            to_head_sha="8" * 40,
            to_route_plan_sha256="3" * 64,
        )
        with_route["migration_history"].insert(-1, route_edge)
        with_route["migration_history"][-1].update(
            from_head_sha="8" * 40,
            from_route_plan_sha256="3" * 64,
        )
        observed = []
        controller.exact_route_migration_commit = (
            lambda _claim, before, after, **kwargs:
            observed.append((before, after, kwargs.get("migration")))
            or kwargs.get("migration") == route_edge
        )
        self.assertEqual(
            len(controller.bundle_refresh_migration_suffix(
                claim, with_route, prior, "7" * 64,
            )),
            4,
        )
        self.assertEqual(observed, [("9" * 40, "8" * 40, route_edge)])
        controller.exact_route_migration_commit = lambda *_args, **_kwargs: False
        self.assertIsNone(controller.bundle_refresh_migration_suffix(
            claim, with_route, prior, "7" * 64,
        ))
        mutations = []
        for name in (
            "first-passport", "gap", "reorder", "duplicate", "head",
            "route", "protected", "missing-final", "extra-final",
            "final-factory", "final-head", "final-route", "final-protected",
            "final-file", "final-digest", "history",
        ):
            invalid = copy.deepcopy(passport)
            edges = invalid["migration_history"]
            if name == "first-passport":
                edges[0]["from_passport_file_sha256"] = "0" * 64
            elif name == "gap":
                edges[1]["from_factory_sha"] = "c" * 40
            elif name == "reorder":
                invalid["migration_history"] = list(reversed(edges))
            elif name == "duplicate":
                invalid["migration_history"] = [edges[0], *edges]
            elif name == "head":
                edges[1]["from_head_sha"] = "c" * 40
            elif name == "route":
                edges[1]["from_route_plan_sha256"] = "0" * 64
            elif name == "protected":
                edges[1]["from_protected_base_sha"] = "c" * 40
            elif name == "missing-final":
                edges.pop()
            elif name == "extra-final":
                edges.append(copy.deepcopy(edges[-1]))
            elif name == "final-factory":
                edges[-1]["from_factory_sha"] = "0" * 40
            elif name == "final-head":
                edges[-1]["to_head_sha"] = edges[-1]["from_head_sha"]
            elif name == "final-route":
                edges[-1]["to_route_plan_sha256"] = edges[-1][
                    "from_route_plan_sha256"
                ]
            elif name == "final-protected":
                edges[-1]["from_protected_base_sha"] = "c" * 40
            elif name == "final-file":
                invalid["parent_file_sha256"] = "0" * 64
            elif name == "final-digest":
                invalid["parent_digest"] = "0" * 64
            else:
                invalid["factory_release_history"].insert(
                    1, dict(invalid["factory_release_history"][0]),
                )
            mutations.append((name, invalid))
        for name, invalid in mutations:
            with self.subTest(name=name):
                self.assertIsNone(
                    controller.bundle_refresh_migration_suffix(
                        claim, invalid, prior, "7" * 64,
                    )
                )

    def test_bundle_refresh_accepts_only_exact_lagged_route_migration(self) -> None:
        controller = CONTROL.Controller(self.args)
        ticket = "T-110"
        source = "b" * 40
        target = self.release.name
        cell = self.root / "cell-bundle-route"
        subprocess.run(
            ["git", "init", "-q", "-b", f"ticket/{ticket}", str(cell)],
            check=True,
        )
        ticket_path = cell / f"factory/tickets/{ticket}.md"
        route_path = cell / f"factory/route-plans/{ticket}.json"
        pin_path = cell / "factory/KIT_PIN"
        ticket_path.parent.mkdir(parents=True)
        route_path.parent.mkdir(parents=True)
        ticket_path.write_text(
            f"State: Awaiting Approval\nKit-SHA: {source}\n",
            encoding="utf-8",
        )
        pin_path.write_text(source + "\n", encoding="utf-8")
        route_path.write_text(CONTROL.canonical({
            "kit_sha": source, "schema": "ticket-model-route-plan/v1",
            "ticket": ticket,
        }) + "\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(cell), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(cell), "-c", "user.name=Fixture",
            "-c", "user.email=fixture@example.invalid", "commit", "-qm", "base",
        ], check=True)
        before = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD"], text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        old_route = route_path.read_bytes()
        ticket_path.write_text(
            f"State: Awaiting Approval\nKit-SHA: {target}\n",
            encoding="utf-8",
        )
        pin_path.write_text(target + "\n", encoding="utf-8")
        route_path.write_text(CONTROL.canonical({
            "kit_sha": target,
            "revisions": [{"body": {
                "kind": "migration",
                "legacy_plan_b64": base64.b64encode(old_route).decode(),
                "legacy_plan_sha256": hashlib.sha256(old_route).hexdigest(),
                "new_kit_sha": source, "old_kit_sha": source,
            }}, {"body": {
                "kind": "release-migration", "new_kit_sha": target,
                "old_kit_sha": source,
            }}],
            "schema": "ticket-model-route-journal/v2", "ticket": ticket,
        }) + "\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(cell), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(cell), "-c", "user.name=Software Factory",
            "-c", "user.email=factory@local", "commit", "-qm",
            f"{ticket}: migrate model route journal",
        ], check=True)
        after = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD"], text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        migration = {
            "from_factory_sha": target, "from_head_sha": before,
            "from_passport_file_sha256": "1" * 64,
            "from_passport_sha256": "2" * 64,
            "from_protected_base_sha": "3" * 40,
            "from_route_plan_sha256": hashlib.sha256(old_route).hexdigest(),
            "schema": CONTROL.PASSPORT_MIGRATION_SCHEMA,
            "to_factory_sha": target, "to_head_sha": after,
            "to_protected_base_sha": "3" * 40,
            "to_route_plan_sha256": hashlib.sha256(
                route_path.read_bytes()
            ).hexdigest(),
        }
        claim = {"ticket": ticket, "worktree": str(cell)}

        self.assertTrue(controller.exact_route_migration_commit(
            claim, before, after, migration=migration,
        ))
        wrong = dict(migration, from_route_plan_sha256="4" * 64)
        self.assertFalse(controller.exact_route_migration_commit(
            claim, before, after, migration=wrong,
        ))
        controller.cell_git = lambda item, *args: (
            subprocess.CompletedProcess(args, 0, source + "\n", "")
            if args == ("show", f"{after}:factory/KIT_PIN")
            else CONTROL.Controller.cell_git(item, *args)
        )
        self.assertFalse(controller.exact_route_migration_commit(
            claim, before, after, migration=migration,
        ))
        controller.cell_git = CONTROL.Controller.cell_git
        cell_git = controller.cell_git
        controller.cell_git = lambda item, *args: (
            subprocess.CompletedProcess(args, 0, "[]\n", "")
            if args == ("show", f"{after}:{route_path.relative_to(cell)}")
            else cell_git(item, *args)
        )
        self.assertFalse(controller.exact_route_migration_commit(
            claim, before, after, migration=migration,
        ))

    def test_bundle_refresh_receipt_handoff_refuses_unbound_evidence(self) -> None:
        prior = "f" * 40
        lease = "a" * 64
        controller = CONTROL.Controller(self.args)
        controller.role_active = lambda _claim: False

        for ticket in ("T-110", "T-111", "T-112", "T-113"):
            (self.root / f"parked/{ticket}").mkdir(parents=True)
            _, current_file = self.migrated_bundle_passport(ticket, prior)
            stale = self.stale_release_receipt(
                ticket, prior, lease,
                stage=(
                    "AWAIT-MERGE approval attested; protected auto-merge request "
                    "pending"
                    if ticket == "T-113" else
                    "AWAIT-OPERATOR bundle attested; await operator approval"
                ),
            )
            claim = {
                "branch": f"ticket/{ticket}", "lease": lease,
                "priority": "normal", "publication_lease": "",
                "receipt": "", "role": "", "schema": CONTROL.CLAIM_SCHEMA,
                "status": "blocked", "ticket": ticket,
                "worktree": str(self.root / f"parked/{ticket}"),
            }
            if ticket == "T-110":
                stale["passport_sha256"] = "8" * 64
                stale["receipt_sha256"] = hashlib.sha256(
                    CONTROL.canonical_document({
                        key: item for key, item in stale.items()
                        if key not in {
                            "consumed", "consumed_at_epoch", "receipt_sha256",
                        }
                    })
                ).hexdigest()
                CONTROL.write(self.state / f"{ticket}.json", stale)
            elif ticket != "T-113":
                parent = stale["receipt_sha256"]
                stale.update(
                    factory_sha=self.release.name,
                    head_sha="c" * 40,
                    parent_digest=parent,
                    passport_sha256=current_file,
                    route_plan_sha256="d" * 64,
                    stage="AWAIT-OPERATOR operator approval observed",
                )
                if ticket == "T-112":
                    stale["lease_sha256"] = "0" * 64
                stale.pop("receipt_sha256")
                stale["receipt_sha256"] = hashlib.sha256(STATE.canonical({
                    key: item for key, item in stale.items()
                    if key not in {
                        "consumed", "consumed_at_epoch", "receipt_sha256",
                    }
                })).hexdigest()
                CONTROL.write(self.state / f"{ticket}.json", stale)
                controller.marker(
                    f"bundle-refresh-transition-{ticket}-{self.release.name}",
                    {
                        "factory_sha": self.release.name,
                        "from_factory_sha": prior,
                        "from_passport_file_sha256": "7" * 64,
                        "from_receipt_sha256": (
                            "0" * 64 if ticket == "T-111" else parent
                        ),
                        "head_sha": "c" * 40,
                        "lease_sha256": hashlib.sha256(
                            lease.encode()
                        ).hexdigest(),
                        "passport_file_sha256": current_file,
                        "route_plan_sha256": "d" * 64,
                        "schema": CONTROL.EVENT_SCHEMA,
                        "ticket": ticket,
                    },
                )
            before = (self.state / f"{ticket}.json").read_bytes()
            marker = (
                self.state / f"bundle-refresh-transition-{ticket}-"
                f"{self.release.name}.json"
            )
            marker_before = marker.read_bytes() if marker.exists() else None
            with self.assertRaises(CONTROL.ControllerError):
                controller.refresh_prior_release_receipt(claim)
            self.assertEqual((self.state / f"{ticket}.json").read_bytes(), before)
            self.assertEqual(
                marker.read_bytes() if marker.exists() else None,
                marker_before,
            )
            if ticket == "T-112":
                released = []
                controller.release_ticket_lease = released.append
                controller.release_inactive_ticket_leases([claim])
                self.assertEqual(released, [claim])
                claim.update(
                    lease="b" * 64, lease_released=True, status="waiting",
                )
                self.assertFalse(controller.bundle_refresh_handoff_pending(
                    claim, rotated_lease=True,
                ))

    def test_release_bundle_refresh_returns_to_route_migration_gate(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "parked/T-110"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        claim = {
            "branch": "ticket/T-110", "lease": "a" * 64,
            "priority": "normal", "publication_lease": "", "receipt": "",
            "release_refresh_required": True, "role": "",
            "schema": CONTROL.CLAIM_SCHEMA, "status": "claimed",
            "ticket": "T-110", "worktree": str(cell),
        }
        controller.save_claim(claim)
        calls = []

        def json_call(*args, **_kwargs):
            calls.append(args)
            if args[0] == "state-machine":
                return state_transition(
                    "REFUSE ticket Kit-SHA lease does not match the selected kit SHA"
                )
            if args[0] == "ticket-attest":
                return {"action": "refresh", "head": "d" * 40}
            if args[:2] == ("publication", "withdraw"):
                return {"status": "absent"}
            return {}

        controller.json_call = json_call
        controller.finish_pending_run = lambda _claim: True
        controller.refresh_dependency_tracking = lambda _claim: True
        controller.ticket_merged = lambda _claim: False
        controller.migrate_passport = lambda *_args: self.fail(
            "Kit-SHA refusal must wait for route migration"
        )

        self.assertEqual(
            controller.reconcile_ticket(claim),
            {"status": "blocked", "ticket": "T-110"},
        )
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(claim["blocked_reason"], "route-migration-required")
        self.assertNotIn("release_refresh_required", claim)
        self.assertEqual(
            sum(call[0] == "ticket-attest" for call in calls if call), 0
        )

    def test_release_upgrade_recovery_overlaps_independent_tickets(self) -> None:
        import threading

        controller = CONTROL.Controller(self.args)
        claims = [{"ticket": f"T-{number}"} for number in range(110, 113)]
        barrier = threading.Barrier(len(claims))

        def recovery(items):
            self.assertEqual(len(items), 1)
            barrier.wait(timeout=2)

        controller.recover_each(
            claims, recovery, "release-upgrade", concurrent=True
        )

    def test_release_upgrade_recovers_prior_role_failure_after_migration(self) -> None:
        controller = CONTROL.Controller(self.args)
        source = "b" * 40
        receipt = "c" * 64
        passport_path = self.state / "passports/T-110.json"
        passport_path.parent.mkdir(mode=0o700)
        CONTROL.write(passport_path, {"factory_sha": source})
        claim = {
            "blocked_reason": "role-failure",
            "branch": "ticket/T-110",
            "lease": "d" * 64,
            "receipt": receipt,
            "role": "spec-linter",
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(self.product),
        }
        terminal = {
            "kit_sha": source,
            "role_exit": "role_exit_protected_ticket_mutation",
        }
        controller.terminal_for_receipt = (
            lambda _ticket, value: terminal if value == receipt else None
        )
        controller.authenticated_operator_passport = lambda _ticket: None
        controller.quarantine_legacy_protected_mutation = (
            lambda _claim, _terminal: False
        )
        controller.ticket_release_current = lambda _claim: True
        controller.renew = lambda _claim: None
        controller.migrate_passport = lambda _claim, _mode: CONTROL.write(
            passport_path, {"factory_sha": self.release.name}
        )
        recovered = []

        def recover(items):
            recovered.append(items[0]["ticket"])
            items[0].update(receipt="", role="", status="claimed")

        controller.recover_repaired_failures = recover
        controller.recover_upgraded_claims([claim])

        self.assertEqual(recovered, ["T-110"])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual((claim["receipt"], claim["role"]), ("", ""))

        recovered.clear()
        claim.update(
            blocked_reason="role-failure", receipt=receipt,
            role="spec-linter", status="blocked",
        )
        terminal["role_exit"] = "provider_failed"
        CONTROL.write(passport_path, {"factory_sha": source})
        controller.recover_upgraded_claims([claim])
        self.assertEqual(recovered, [])
        self.assertEqual((claim["status"], claim["receipt"]), ("blocked", receipt))

    def test_prior_role_failure_accepts_only_exact_route_migration_suffix(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        source = "b" * 40
        intermediate = "c" * 40
        input_head, route_head, current_head = (value * 40 for value in "def")
        protected = "1" * 40
        old_route, middle_route, current_route = (value * 64 for value in "234")
        receipt = "5" * 64

        def edge(
            before_factory, after_factory, before_head, after_head,
            before_route, after_route, parent_file, parent_digest,
        ):
            return {
                "from_factory_sha": before_factory,
                "from_head_sha": before_head,
                "from_passport_file_sha256": parent_file,
                "from_passport_sha256": parent_digest,
                "from_protected_base_sha": protected,
                "from_route_plan_sha256": before_route,
                "schema": CONTROL.PASSPORT_MIGRATION_SCHEMA,
                "to_factory_sha": after_factory,
                "to_head_sha": after_head,
                "to_protected_base_sha": protected,
                "to_route_plan_sha256": after_route,
            }

        migrations = [
            edge(
                source, intermediate, input_head, input_head,
                old_route, old_route, "6" * 64, "7" * 64,
            ),
            edge(
                intermediate, self.release.name, input_head, route_head,
                old_route, middle_route, "8" * 64, "9" * 64,
            ),
            edge(
                self.release.name, self.release.name, route_head, current_head,
                middle_route, current_route, "a" * 64, "b" * 64,
            ),
        ]
        passport = {
            "branch": "ticket/T-110",
            "charge_records": [],
            "completed_role_evidence": [],
            "factory_release_history": [
                {"contract_version": "2.0.0", "factory_sha": source},
                {"contract_version": "2.0.0", "factory_sha": intermediate},
                {"contract_version": "2.0.0", "factory_sha": self.release.name},
            ],
            "factory_sha": self.release.name,
            "head_sha": current_head,
            "migration_history": migrations,
            "parent_digest": "b" * 64,
            "parent_file_sha256": "a" * 64,
            "protected_base_sha": protected,
            "route_plan_sha256": current_route,
            "ticket": "T-110",
            "transition_receipt_sha256": receipt,
        }
        claim = {
            "branch": "ticket/T-110", "lease": "c" * 64,
            "lease_released": True, "parked": True, "priority": "normal",
            "publication_lease": "", "receipt": receipt,
            "role": "spec-linter", "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked", "ticket": "T-110",
            "worktree": str(self.product),
        }
        terminal = {
            "accounting_state": "abandoned_conservative",
            "cost_basis": "conservative_reservation",
            "effective_cost": "2.000000", "exit_status": "11",
            "go_issued": "1", "kit_sha": source, "phase": "completed",
            "reserved_usd": "2.000000", "role": "spec-linter",
            "role_exit": "role_exit_protected_ticket_mutation",
            "role_head_before": input_head, "run_id": "failed-lint",
            "task_submitted": "1",
        }
        pairs = []
        controller.exact_route_migration_commit = (
            lambda _claim, before, after: pairs.append((before, after))
            or (before, after) in {
                (input_head, route_head), (route_head, current_head),
            }
        )
        controller.remote_passport_valid = lambda _claim: True
        self.assertTrue(
            controller.route_migrated_failed_role(claim, terminal, passport)
        )
        self.assertEqual(
            pairs,
            [(input_head, route_head), (route_head, current_head)],
        )

        drifted = copy.deepcopy(passport)
        drifted["migration_history"][1]["to_head_sha"] = "0" * 40
        self.assertFalse(
            controller.route_migrated_failed_role(claim, terminal, drifted)
        )

        passport["charge_records"] = [{
            "role": "spec-linter", "run_id": "failed-lint",
            "transition_receipt_sha256": receipt,
        }]
        passport_path = self.state / "passports/T-110.json"
        passport_path.parent.mkdir(mode=0o700)
        CONTROL.write(passport_path, passport)
        controller.restore_recorded_contract_repair = lambda _claim: False
        controller.restore_contract_blocker = lambda _claim: False
        controller.role_active = lambda _claim: False
        controller.direct_model_identity_candidate = lambda *_args: False
        controller.terminal_for_receipt = lambda *_args: terminal
        controller.terminal_already_exported = lambda *_args: True
        controller.exact_semantic_authorization_recovery = lambda *_args: False
        controller.ensure_lease = lambda *_args: None
        events = []
        controller.event = lambda name, *_args, **_kwargs: events.append(name)
        controller.recover_repaired_failures([claim])
        self.assertEqual((claim["status"], claim["role"], claim["receipt"]), (
            "claimed", "", "",
        ))
        self.assertEqual(events, [
            "protected_ticket_mutation_recovered_by_release_upgrade",
        ])

    def test_release_upgrade_attempt_retains_discovered_route_wait(self) -> None:
        controller = CONTROL.Controller(self.args)
        source = "b" * 40
        passport_path = self.state / "passports/T-110.json"
        passport_path.parent.mkdir(mode=0o700)
        CONTROL.write(passport_path, {"factory_sha": source, "head_sha": ""})
        claim = {
            "branch": "ticket/T-110",
            "receipt": "c" * 64,
            "role": "spec-linter",
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(self.product),
        }
        terminal = {
            "kit_sha": "d" * 40,
            "role_exit": "role_exit_protected_ticket_mutation",
        }
        controller.terminal_for_receipt = lambda _ticket, _receipt: terminal
        controller.authenticated_operator_passport = lambda _ticket: None
        quarantined = []
        controller.quarantine_legacy_protected_mutation = (
            lambda _claim, _terminal: quarantined.append(True)
        )
        controller.ticket_release_current = lambda _claim: False
        controller.migrate_passport = lambda _claim, _mode: None
        controller.recovery_input_sha256 = lambda _claim, _name: "e" * 64
        controller.recover_each(
            [claim], controller.recover_upgraded_claims, "release-upgrade",
        )

        self.assertEqual(claim["blocked_reason"], "route-migration-required")
        self.assertEqual(
            claim["recovery_attempt"]["retry_reason"],
            "route-migration-required",
        )
        self.assertEqual(quarantined, [])


    def test_recovery_failures_dedupe_and_release_new_nonlive_lease(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = {
            "lease": "", "lease_released": True, "parked": True,
            "status": "blocked", "ticket": "T-110",
        }
        released = []
        controller.role_active = lambda _claim: False
        controller.release_ticket_lease = lambda item: (
            released.append(item["lease"]), item.update(lease_released=True)
        )[-1]

        def fail(message):
            def recovery(items):
                items[0].update(lease="f" * 64)
                items[0].pop("lease_released", None)
                raise CONTROL.ControllerError(message)
            controller.recover_each([claim], recovery, "release-upgrade")

        fail("token=never-print\nrecovery kind detached")
        fail("token=never-print\nrecovery kind detached")
        fail("token=never-print\nrecovery kind changed")

        events = [
            CONTROL.read(path) for path in controller.events.glob("*.json")
            if CONTROL.read(path).get("event") == "ticket_recovery_failed"
        ]
        self.assertEqual(len(events), 2)
        self.assertTrue(all("never-print" not in item["error"] for item in events))
        self.assertEqual(len(released), 3)

    def test_external_recovery_wait_does_not_consume_retry_budget(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = self.recovery_claim()
        controller.role_active = lambda _claim: False
        controller.recovery_input_sha256 = lambda *_args: "a" * 64

        controller.recover_each(
            [claim],
            lambda _items: (_ for _ in ()).throw(
                CONTROL.ExternalUnavailable()
            ),
            "prepublication-attestation",
        )

        self.assertEqual(claim["blocked_reason"], "external-unavailable")
        self.assertNotIn("recovery_attempt", claim)
        self.assertFalse(any(
            CONTROL.read(path).get("event") == "ticket_recovery_failed"
            for path in controller.events.glob("*.json")
        ))

    def test_recovery_error_redaction_covers_structured_secrets(self) -> None:
        error = CONTROL.ControllerError(
            "Authorization: Bearer SUPERSECRET\n"
            "auth=\"two word secret\"\n"
            "password: |\n  continued secret\n"
            "endpoint=https://user:pass@example.invalid/path\n"
            "recovery kind detached"
        )
        detail = CONTROL.safe_error(error)
        for secret in (
            "SUPERSECRET", "two word secret", "continued secret",
            "user:pass@example.invalid",
        ):
            self.assertNotIn(secret, detail)
        self.assertIn("recovery kind detached", detail)

    def test_successful_parked_recovery_retains_new_lease(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = {
            "lease": "", "lease_released": True, "parked": True,
            "status": "blocked", "ticket": "T-110",
        }
        released = []
        controller.role_active = lambda _claim: False
        controller.release_ticket_lease = lambda item: released.append(item)

        def recovery(items):
            items[0].update(lease="f" * 64, status="claimed")
            items[0].pop("lease_released", None)

        controller.recover_each([claim], recovery, "release-upgrade")

        self.assertEqual(released, [])
        self.assertEqual(claim["lease"], "f" * 64)
        self.assertNotIn("lease_released", claim)

    def test_identical_recovery_failure_abandons_once_and_survives_restart(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = self.recovery_claim()
        claim["lease_released"] = True
        controller.save_claim(claim)
        calls = []

        def fail(items):
            calls.append(items[0]["ticket"])
            raise CONTROL.ControllerError("same invariant")

        controller.withdraw_publication = lambda _claim: None
        for _ in range(CONTROL.RECOVERY_ATTEMPT_LIMIT):
            controller.recover_each([claim], fail, "release-upgrade")

        self.assertEqual(len(calls), CONTROL.RECOVERY_ATTEMPT_LIMIT)
        self.assertEqual(
            claim["blocked_reason"], "recovery-abandoned:release-upgrade"
        )
        self.assertTrue(claim["lease_released"])
        restarted = CONTROL.Controller(self.args)
        persisted = restarted.load_claims()[0]
        cleanup = []
        restarted.withdraw_publication = lambda _claim: cleanup.append("withdraw")
        hashes = []
        recovery_input_sha256 = restarted.recovery_input_sha256
        restarted.recovery_input_sha256 = lambda item, name: (
            hashes.append(name) or recovery_input_sha256(item, name)
        )
        event_count = len(list(restarted.events.glob("*.json")))
        selectors = (
            "interrupted-reconciliation", "missing-terminal",
            "passportless-route-migration", "preflight-retry",
            "release-upgrade", "terminal-export", "targeted-repair",
            "prepublication-attestation",
        )
        for _ in range(2):
            for selector in selectors:
                restarted.recover_each([persisted], fail, selector)
        events = [
            CONTROL.read(path) for path in restarted.events.glob("*.json")
            if CONTROL.read(path).get("event") == "ticket_recovery_abandoned"
        ]
        self.assertEqual(len(calls), CONTROL.RECOVERY_ATTEMPT_LIMIT)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["attempts"], CONTROL.RECOVERY_ATTEMPT_LIMIT)
        self.assertEqual(len(list(restarted.events.glob("*.json"))), event_count)
        self.assertEqual(cleanup, [])
        self.assertEqual(hashes, ["release-upgrade", "release-upgrade"])

    def test_abandonment_restart_finishes_after_lease_release_save(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = self.recovery_claim()
        controller.save_claim(claim)
        controller.withdraw_publication = lambda _claim: None

        def fail(_items):
            raise CONTROL.ControllerError("same invariant")

        controller.recover_each([claim], fail, "release-upgrade")
        controller.recover_each([claim], fail, "release-upgrade")
        controller.json_call = lambda *_args, **_kwargs: {"absent": True}
        original_release = controller.release_ticket_lease

        def release_then_crash(item):
            original_release(item)
            raise RuntimeError("crash after lease release save")

        controller.release_ticket_lease = release_then_crash
        with self.assertRaisesRegex(RuntimeError, "crash after lease release save"):
            controller.recover_each([claim], fail, "release-upgrade")
        persisted = CONTROL.read(controller.claim_path("T-110"))
        self.assertEqual(
            persisted["blocked_reason"], "recovery-abandoned:release-upgrade"
        )
        self.assertTrue(persisted["lease_released"])
        restarted = CONTROL.Controller(self.args)
        cleanup = []
        restarted.withdraw_publication = lambda _claim: cleanup.append("withdraw")
        restarted.recover_each([persisted], fail, "release-upgrade")
        restarted.recover_each([persisted], fail, "targeted-repair")
        events = [
            CONTROL.read(path) for path in restarted.events.glob("*.json")
            if CONTROL.read(path).get("event") == "ticket_recovery_abandoned"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(cleanup, ["withdraw"])

    def test_abandonment_persists_before_no_lease_publication_refusal(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = self.recovery_claim()
        claim["lease_released"] = True
        controller.save_claim(claim)
        attempts = []

        def fail(_items):
            attempts.append("attempt")
            raise CONTROL.ControllerError("same invariant")

        controller.withdraw_publication = lambda _claim: (_ for _ in ()).throw(
            CONTROL.ControllerError("publication unavailable")
        )
        for _ in range(CONTROL.RECOVERY_ATTEMPT_LIMIT):
            controller.recover_each([claim], fail, "release-upgrade")
        persisted = CONTROL.read(controller.claim_path("T-110"))
        self.assertEqual(
            persisted["blocked_reason"], "recovery-abandoned:release-upgrade"
        )
        self.assertEqual(persisted["recovery_attempt"]["phase"], "abandoning")

        restarted = CONTROL.Controller(self.args)
        cleanup = []

        def cleanup_fails(_claim):
            cleanup.append("withdraw")
            raise CONTROL.ControllerError("still unavailable")

        restarted.withdraw_publication = cleanup_fails
        selectors = (
            "interrupted-reconciliation", "missing-terminal",
            "passportless-route-migration", "preflight-retry",
            "release-upgrade", "terminal-export", "targeted-repair",
            "prepublication-attestation",
        )
        for _ in range(2):
            for selector in selectors:
                restarted.recover_each([persisted], fail, selector)
        self.assertEqual(cleanup, ["withdraw", "withdraw"])
        restarted.withdraw_publication = lambda _claim: cleanup.append("withdraw")
        restarted.recover_each([persisted], fail, "release-upgrade")
        restarted.recover_each([persisted], fail, "targeted-repair")
        events = [
            CONTROL.read(path) for path in restarted.events.glob("*.json")
            if CONTROL.read(path).get("event") == "ticket_recovery_abandoned"
        ]
        self.assertEqual(len(attempts), CONTROL.RECOVERY_ATTEMPT_LIMIT)
        self.assertEqual(len(events), 1)
        self.assertEqual(cleanup, ["withdraw", "withdraw", "withdraw"])

    def test_apparent_recovery_round_trip_is_bounded(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = self.recovery_claim()
        claim["lease_released"] = True
        controller.save_claim(claim)
        controller.withdraw_publication = lambda _claim: None
        controller.park_claim = lambda _claim: True

        def recover(items):
            items[0].update(status="claimed")
            items[0].pop("blocked_reason", None)
            controller.save_claim(items[0])

        def round_trip(item):
            item.update(status="blocked", blocked_reason="retryable")
            controller.save_claim(item)
            return {"status": "blocked", "ticket": item["ticket"]}

        controller.reconcile_ticket = round_trip
        for _ in range(CONTROL.RECOVERY_ATTEMPT_LIMIT):
            controller.recover_each([claim], recover, "targeted-repair")
            controller.reconcile_ticket_until_wait(claim)

        self.assertEqual(
            claim["blocked_reason"], "recovery-abandoned:targeted-repair"
        )
        self.assertEqual(claim["recovery_attempt"]["count"], 3)

    def test_receipt_bound_recovery_wait_is_free_and_ticket_local(self) -> None:
        receipt = "b" * 64

        def waiting_recovery(controller, items):
            claim = items[0]
            controller.save_claim(claim)
            controller.wait_for_recovery_receipt(claim)

        controller = CONTROL.Controller(self.args)
        claim = self.recovery_claim()
        claim.update(receipt=receipt, role="planner", lease_released=True)
        controller.save_claim(claim)
        controller.operator_transition = lambda _claim: {
            "consumed": False, "receipt_sha256": receipt,
        }
        for _ in range(7):
            controller.recover_each(
                [claim], lambda items: waiting_recovery(controller, items),
                "targeted-repair",
            )
        self.assertNotIn("recovery_attempt", claim)

        restarted = CONTROL.Controller(self.args)
        persisted = restarted.load_claims()[0]
        restarted.operator_transition = controller.operator_transition
        for _ in range(2):
            restarted.recover_each(
                [persisted], lambda items: waiting_recovery(restarted, items),
                "targeted-repair",
            )
        self.assertNotIn("recovery_attempt", persisted)

        for name, current in (
            ("stale", None),
            ("consumed", {"consumed": True, "receipt_sha256": receipt}),
            ("mismatched", {
                "consumed": False, "receipt_sha256": "c" * 64,
            }),
        ):
            with self.subTest(invalid_wait=name):
                invalid = self.recovery_claim(f"T-11{len(name)}")
                invalid.update(
                    receipt=receipt, role="planner", lease_released=True,
                )
                controller.operator_transition = lambda _claim, value=current: value
                controller.withdraw_publication = lambda _claim: None
                for _ in range(CONTROL.RECOVERY_ATTEMPT_LIMIT):
                    controller.recover_each(
                        [invalid],
                        lambda items: waiting_recovery(controller, items),
                        "targeted-repair",
                    )
                self.assertEqual(
                    invalid["blocked_reason"],
                    "recovery-abandoned:targeted-repair",
                )
                self.assertEqual(
                    invalid["recovery_attempt"]["count"],
                    CONTROL.RECOVERY_ATTEMPT_LIMIT,
                )

        uncertain = self.recovery_claim("T-113")
        uncertain.update(
            receipt=receipt, role="planner", lease_released=True,
        )
        checks = iter((
            {"consumed": False, "receipt_sha256": receipt},
            CONTROL.ControllerError("transition revalidation failed"),
        ))

        def uncertain_transition(_claim):
            result = next(checks)
            if isinstance(result, Exception):
                raise result
            return result

        controller.operator_transition = uncertain_transition
        controller.recover_each(
            [uncertain], lambda items: waiting_recovery(controller, items),
            "targeted-repair",
        )
        self.assertEqual(
            (uncertain["recovery_attempt"]["phase"],
             uncertain["recovery_attempt"]["count"]),
            ("settled", 1),
        )

        sibling = self.recovery_claim("T-112")
        sibling.update(receipt="d" * 64, role="builder", lease_released=True)
        claim.pop("recovery_attempt", None)
        controller.operator_transition = lambda item: (
            {"consumed": False, "receipt_sha256": receipt}
            if item["ticket"] == claim["ticket"] else None
        )

        def concurrent_recovery(items):
            item = items[0]
            controller.save_claim(item)
            if item["ticket"] == claim["ticket"]:
                controller.wait_for_recovery_receipt(item)

        controller.recover_each(
            [claim, sibling], concurrent_recovery, "targeted-repair",
            concurrent=True,
        )
        self.assertNotIn("recovery_attempt", claim)
        self.assertEqual(sibling["recovery_attempt"]["count"], 1)

    def test_receipt_bound_wait_is_durable_before_recovery_returns(self) -> None:
        controller = CONTROL.Controller(self.args)
        receipt = "b" * 64
        claim = self.recovery_claim()
        claim.update(receipt=receipt, role="planner", lease_released=True)
        controller.operator_transition = lambda _claim: {
            "consumed": False, "receipt_sha256": receipt,
        }

        controller.recover_each(
            [claim],
            lambda _items: (_ for _ in ()).throw(
                CONTROL.ControllerError("genuine failure")
            ),
            "targeted-repair",
        )
        prior = dict(claim["recovery_attempt"])
        self.assertEqual((prior["phase"], prior["count"]), ("settled", 1))

        def crash_after_wait(items):
            item = items[0]
            controller.save_claim(item)
            self.assertTrue(controller.wait_for_recovery_receipt(item))
            raise KeyboardInterrupt("crash after wait recognition")

        with self.assertRaisesRegex(KeyboardInterrupt, "wait recognition"):
            controller.recover_each(
                [claim], crash_after_wait, "targeted-repair",
            )
        self.assertEqual(
            CONTROL.read(controller.claim_path(claim["ticket"]))[
                "recovery_attempt"
            ],
            prior,
        )

        restarted = CONTROL.Controller(self.args)
        persisted = restarted.load_claims()[0]
        restarted.operator_transition = controller.operator_transition

        def wait_again(items):
            item = items[0]
            restarted.save_claim(item)
            self.assertTrue(restarted.wait_for_recovery_receipt(item))

        restarted.recover_each(
            [persisted], wait_again, "targeted-repair",
        )
        self.assertEqual(persisted["recovery_attempt"], prior)

    def test_contract_block_operator_wait_does_not_spend_attempts(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = self.recovery_claim()
        receipt = "b" * 64
        claim.update(
            receipt=receipt, role="planner", lease_released=True,
        )
        ticket = Path(claim["worktree"]) / "factory/tickets/T-110.md"
        ticket.write_text(
            "State: Blocked-Escalated\n"
            "OPERATOR ANSWER: Preserve the exact seam.\n"
            f"OPERATOR ANSWER RECEIPT: {receipt}\n",
            encoding="utf-8",
        )
        (self.state / "passports").mkdir(mode=0o700)
        CONTROL.write(self.state / "passports/T-110.json", {"ticket": "T-110"})
        controller.save_claim(claim)
        controller.restore_recorded_contract_repair = lambda _claim: False
        controller.restore_contract_blocker = lambda _claim: None
        controller.terminal_for_receipt = lambda *_args: {
            "exit_status": "12", "role_exit": "role_exit_contract_blocked",
            "run_id": "contract-block",
        }
        controller.direct_model_identity_candidate = lambda *_args: False
        controller.remote_passport_valid = lambda _claim: True
        controller.operator_transition = lambda _claim: {
            "consumed": False, "receipt_sha256": receipt,
        }

        def ensure(item, _label):
            item["lease"] = "c" * 64
            item.pop("lease_released", None)
            controller.save_claim(item)

        def release(item):
            item["lease_released"] = True
            controller.save_claim(item)

        controller.ensure_lease = ensure
        controller.release_ticket_lease = release
        controller.json_call = lambda *args, **_kwargs: (
            {"status": "blocked"}
            if args[:2] == ("state-machine", "block") else self.fail(args)
        )
        for _ in range(7):
            controller.recover_each(
                [claim], controller.recover_repaired_failures,
                "targeted-repair",
            )
        self.assertNotIn("recovery_attempt", claim)
        self.assertNotIn("recovery_attempt", CONTROL.read(
            controller.claim_path("T-110")
        ))

    def test_recovery_limit_resets_on_error_and_authenticated_head_change(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = self.recovery_claim()
        claim["lease_released"] = True
        controller.save_claim(claim)

        def fail(message):
            controller.recover_each(
                [claim],
                lambda _items: (_ for _ in ()).throw(
                    CONTROL.ControllerError(message)
                ),
                "release-upgrade",
            )

        fail("first")
        fail("second")
        self.assertEqual(claim["recovery_attempt"]["count"], 1)
        fail("second")
        self.assertEqual(claim["recovery_attempt"]["count"], 2)
        cell = Path(claim["worktree"])
        (cell / "tracked").write_text("operator repair\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(cell), "add", "tracked"], check=True)
        subprocess.run([
            "git", "-C", str(cell), "-c", "user.name=Operator",
            "-c", "user.email=operator@example.invalid", "commit", "-qm", "repair",
        ], check=True)
        fail("second")
        self.assertEqual(claim["recovery_attempt"]["count"], 1)

    def test_recovery_limit_resets_on_receipt_and_exact_ticket_bytes(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = self.recovery_claim()
        claim["lease_released"] = True

        def fail():
            controller.recover_each(
                [claim],
                lambda _items: (_ for _ in ()).throw(
                    CONTROL.ControllerError("same invariant")
                ),
                "release-upgrade",
            )

        fail()
        fail()
        claim["receipt"] = "c" * 64
        fail()
        self.assertEqual(claim["recovery_attempt"]["count"], 1)
        fail()
        ticket_path = (
            Path(claim["worktree"]) / "factory/tickets/T-110.md"
        )
        ticket_path.write_text("State: Ready\nDetail: first\n", encoding="utf-8")
        fail()
        self.assertEqual(claim["recovery_attempt"]["count"], 1)
        fail()
        ticket_path.write_text("State: Ready\nDetail: other\n", encoding="utf-8")
        fail()
        self.assertEqual(claim["recovery_attempt"]["count"], 1)

    def test_exact_qualification_resume_readmits_abandoned_repair(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {"mode": "successor"}
        claim = self.recovery_claim()
        claim.update(
            blocked_reason="recovery-abandoned:targeted-repair",
            lease_released=True, receipt="c" * 64, role="test-author",
            status="blocked",
        )
        operator_map = self.state.parent / "operator/operator-map.json"
        operator_map.parent.mkdir(mode=0o700)

        def abandon() -> None:
            claim["recovery_attempt"] = {
                "count": CONTROL.RECOVERY_ATTEMPT_LIMIT,
                "factory_sha": self.release.name,
                "input_sha256": controller.recovery_input_sha256(
                    claim, "targeted-repair",
                ),
                "outcome_sha256": "e" * 64,
                "phase": "abandoned", "recovery": "targeted-repair",
                "retry_reason": "role-failure", "retry_status": "blocked",
            }

        def project(blocked_receipt: str) -> None:
            receipt = STATE.operator_receipt.issue(
                self.state, "T-110", "resume", {
                    "blocked_receipt_sha256": blocked_receipt,
                    "resume_stage": "Building",
                },
            )
            CONTROL.write(operator_map, {"tickets": {"T-110": {"operator": {
                "observed_at": "2026-08-15T00:00:00Z",
                "receipt_sha256": receipt["receipt_sha256"],
                "state": "Building", "state_base": "blocked-escalated",
            }}}})

        with patch.dict(os.environ, {
            "FACTORY_OPERATOR_MAP": str(operator_map),
            "FACTORY_QUALIFICATION_MODE": "isolated",
        }):
            CONTROL.write(operator_map, {"tickets": {}})
            controller.qualification = None
            abandon()
            project(claim["receipt"])
            self.assertTrue(controller.recovery_blocked(
                claim, "targeted-repair",
            ))

            CONTROL.write(operator_map, {"tickets": {}})
            controller.qualification = {"mode": "initial"}
            abandon()
            project(claim["receipt"])
            self.assertTrue(controller.recovery_blocked(
                claim, "targeted-repair",
            ))

            CONTROL.write(operator_map, {"tickets": {}})
            controller.qualification = {"mode": "successor"}
            with patch.dict(os.environ, {
                "FACTORY_QUALIFICATION_MODE": "takeover",
            }):
                abandon()
                project(claim["receipt"])
                self.assertTrue(controller.recovery_blocked(
                    claim, "targeted-repair",
                ))

            CONTROL.write(operator_map, {"tickets": {}})
            abandon()
            self.assertTrue(controller.recovery_blocked(
                claim, "targeted-repair",
            ))
            project("d" * 64)
            self.assertTrue(controller.recovery_blocked(
                claim, "targeted-repair",
            ))
            project(claim["receipt"])
            self.assertFalse(controller.recovery_blocked(
                claim, "targeted-repair",
            ))
            self.assertNotIn("recovery_attempt", claim)
            self.assertEqual(claim["blocked_reason"], "role-failure")

            claim["blocked_reason"] = "recovery-abandoned:targeted-repair"
            abandon()
            self.assertTrue(controller.recovery_blocked(
                claim, "targeted-repair",
            ))

    def test_changed_receipt_readmits_real_release_upgrade_selector(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = self.recovery_claim()
        claim.update(
            blocked_reason="route-migration-required", lease_released=True,
        )
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        CONTROL.write(passports / "T-110.json", {
            "factory_sha": self.release.name,
        })
        for prefix in (
            "passport-route-migration-pending",
            "passport-route-migration-complete",
        ):
            controller.marker(
                f"{prefix}-T-110-{self.release.name}",
                {"factory_sha": self.release.name,
                 "schema": CONTROL.EVENT_SCHEMA, "ticket": "T-110"},
            )
        controller.withdraw_publication = lambda _claim: None
        for _ in range(CONTROL.RECOVERY_ATTEMPT_LIMIT):
            controller.recover_each(
                [claim],
                lambda _items: (_ for _ in ()).throw(
                    CONTROL.ControllerError("same invariant")
                ),
                "release-upgrade",
            )
        self.assertEqual(
            claim["blocked_reason"], "recovery-abandoned:release-upgrade"
        )

        claim["receipt"] = "d" * 64
        controller.ticket_release_current = lambda _claim: True
        controller.release_bundle_refreshable = lambda *_args: False
        controller.terminal_for_receipt = lambda *_args: None
        controller.renew = lambda _claim: None
        controller.event = lambda *_args, **_kwargs: None
        controller.recover_each(
            [claim], controller.recover_upgraded_claims, "release-upgrade",
        )
        self.assertEqual(claim["status"], "claimed")
        self.assertNotIn("blocked_reason", claim)
        self.assertEqual(claim["recovery_attempt"]["phase"], "pending")

    def test_completed_route_migration_readmits_abandoned_upgrade(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = self.recovery_claim()
        source = "9" * 40
        target = "b" * 40
        claim.update({
            "blocked_reason": "recovery-abandoned:release-upgrade",
            "lease_released": True,
            "recovery_attempt": {
                "count": CONTROL.RECOVERY_ATTEMPT_LIMIT,
                "factory_sha": self.release.name,
                "input_sha256": "c" * 64,
                "outcome_sha256": "d" * 64,
                "phase": "abandoned",
                "recovery": "release-upgrade",
                "retry_reason": "route-migration-required",
                "retry_status": "blocked",
            },
        })
        controller.marker(
            f"passport-route-migration-pending-T-110-{self.release.name}",
            {
                "factory_sha": self.release.name,
                "schema": CONTROL.EVENT_SCHEMA,
                "ticket": "T-110",
            },
        )
        controller.terminal_for_receipt = lambda *_args: None
        controller.authenticated_operator_passport = lambda _ticket: {
            "factory_sha": self.release.name,
            "head_sha": source,
        }
        controller.remote_cell_head_status = lambda _claim: (
            "pushed", target, target,
        )
        controller.ticket_release_current = lambda _claim: True
        controller.exact_route_migration_commit = lambda _claim, old, new: (
            old == source and new == target
        )

        self.assertTrue(
            controller.readmit_stranded_route_upgrade(claim, "release-upgrade")
        )
        self.assertEqual(claim["blocked_reason"], "route-migration-required")
        self.assertNotIn("recovery_attempt", claim)

    def test_converged_route_upgrade_readmits_abandoned_upgrade(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = self.recovery_claim()
        head = "9" * 40
        claim.update({
            "blocked_reason": "recovery-abandoned:release-upgrade",
            "lease_released": True,
            "recovery_attempt": {
                "count": CONTROL.RECOVERY_ATTEMPT_LIMIT,
                "factory_sha": self.release.name,
                "input_sha256": "c" * 64,
                "outcome_sha256": "d" * 64,
                "phase": "abandoned",
                "recovery": "release-upgrade",
                "retry_reason": "route-migration-required",
                "retry_status": "blocked",
            },
        })
        controller.marker(
            f"passport-route-migration-pending-T-110-{self.release.name}",
            {
                "factory_sha": self.release.name,
                "schema": CONTROL.EVENT_SCHEMA,
                "ticket": "T-110",
            },
        )
        controller.terminal_for_receipt = lambda *_args: None
        controller.authenticated_operator_passport = lambda _ticket: {
            "factory_sha": self.release.name,
            "head_sha": head,
        }
        controller.remote_cell_head_status = lambda _claim: (
            "pushed", head, head,
        )
        controller.ticket_release_current = lambda _claim: True
        controller.exact_route_migration_commit = lambda *_args: False

        self.assertTrue(
            controller.readmit_stranded_route_upgrade(claim, "release-upgrade")
        )
        self.assertEqual(claim["blocked_reason"], "route-migration-required")
        self.assertNotIn("recovery_attempt", claim)

    def test_successor_release_resets_before_reading_old_malformed_evidence(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = self.recovery_claim()
        claim.update(blocked_reason="route-migration-required", lease_released=True)
        controller.withdraw_publication = lambda _claim: None
        for _ in range(CONTROL.RECOVERY_ATTEMPT_LIMIT):
            controller.recover_each(
                [claim],
                lambda _items: (_ for _ in ()).throw(
                    CONTROL.ControllerError("same invariant")
                ),
                "release-upgrade",
            )
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        (passports / "T-110.json").write_text("not-json\n", encoding="utf-8")
        (passports / "T-110.json").chmod(0o600)
        successor = self.root / ("b" * 40)
        successor.mkdir()
        controller.release_path = successor

        self.assertFalse(controller.recovery_blocked(claim, "release-upgrade"))
        self.assertNotIn("recovery_attempt", claim)
        self.assertEqual(claim["blocked_reason"], "route-migration-required")

    def test_claimed_recovery_restores_exact_status_after_evidence_change(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = self.recovery_claim()
        claim.update(status="claimed", lease_released=True)
        claim.pop("blocked_reason")
        controller.withdraw_publication = lambda _claim: None
        for _ in range(CONTROL.RECOVERY_ATTEMPT_LIMIT):
            controller.recover_each(
                [claim],
                lambda _items: (_ for _ in ()).throw(
                    CONTROL.ControllerError("same invariant")
                ),
                "interrupted-reconciliation",
            )
        claim["receipt"] = "d" * 64
        self.assertFalse(controller.recovery_blocked(
            claim, "interrupted-reconciliation"
        ))
        self.assertEqual(claim["status"], "claimed")
        self.assertNotIn("blocked_reason", claim)

    def test_pending_recovery_restart_settles_before_retry(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = self.recovery_claim()
        claim["lease_released"] = True
        controller.save_claim(claim)

        def recover(items):
            items[0].update(status="claimed")
            items[0].pop("blocked_reason", None)
            controller.save_claim(items[0])

        controller.recover_each([claim], recover, "targeted-repair")
        self.assertEqual(claim["recovery_attempt"]["phase"], "pending")
        restarted = CONTROL.Controller(self.args)
        persisted = restarted.load_claims()[0]
        persisted.update(status="blocked", blocked_reason="retryable")
        restarted.save_claim(persisted)
        calls = []
        restarted.recover_each(
            [persisted], lambda _items: calls.append("retried"),
            "interrupted-reconciliation",
        )
        self.assertEqual(calls, [])
        self.assertEqual(persisted["recovery_attempt"]["count"], 0)
        restarted.recover_each(
            [persisted], lambda _items: calls.append("retried"),
            "targeted-repair",
        )
        self.assertEqual(calls, ["retried"])
        self.assertEqual(persisted["recovery_attempt"]["count"], 1)

    def test_malformed_recovery_evidence_does_not_block_sibling(self) -> None:
        controller = CONTROL.Controller(self.args)
        claims = [self.recovery_claim("T-110"), self.recovery_claim("T-111")]
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        (passports / "T-110.json").write_text("not-json\n", encoding="utf-8")
        (passports / "T-110.json").chmod(0o600)
        ran = []
        controller.recover_each(
            claims, lambda items: ran.append(items[0]["ticket"]),
            "release-upgrade",
        )
        self.assertEqual(ran, ["T-111"])
        self.assertEqual(claims[0]["blocked_reason"], "recovery:release-upgrade")

    def test_invalid_utf8_recovery_evidence_does_not_block_sibling(self) -> None:
        controller = CONTROL.Controller(self.args)
        claims = [self.recovery_claim("T-110"), self.recovery_claim("T-111")]
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        (passports / "T-110.json").write_bytes(b"\xff")
        (passports / "T-110.json").chmod(0o600)
        ran = []
        controller.recover_each(
            claims, lambda items: ran.append(items[0]["ticket"]),
            "release-upgrade",
        )
        self.assertEqual(ran, ["T-111"])
        self.assertEqual(claims[0]["blocked_reason"], "recovery:release-upgrade")

    def test_invalid_evidence_during_settlement_does_not_block_sibling(self) -> None:
        controller = CONTROL.Controller(self.args)
        claims = [self.recovery_claim("T-110"), self.recovery_claim("T-111")]
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        ran = []

        def recovery(items):
            claim = items[0]
            ran.append(claim["ticket"])
            if claim["ticket"] == "T-110":
                (passports / "T-110.json").write_bytes(b"\xff")
                (passports / "T-110.json").chmod(0o600)
                controller.save_claim(claim)

        controller.recover_each(claims, recovery, "release-upgrade")
        waiting = [
            CONTROL.read(path) for path in controller.events.glob("*.json")
            if CONTROL.read(path).get("event")
            == "ticket_recovery_settlement_waiting"
        ]
        self.assertEqual(ran, ["T-110", "T-111"])
        self.assertEqual(claims[0]["recovery_attempt"]["phase"], "pending")
        self.assertEqual(len(waiting), 1)

    def test_acquired_lease_release_refusal_does_not_block_sibling(self) -> None:
        controller = CONTROL.Controller(self.args)
        claims = [self.recovery_claim("T-110"), self.recovery_claim("T-111")]
        for claim in claims:
            claim["lease_released"] = True
        ran = []
        released = []

        def recovery(items):
            claim = items[0]
            ran.append(claim["ticket"])
            claim["lease"] = "f" * 64
            claim.pop("lease_released", None)
            controller.save_claim(claim)

        def release(claim):
            if claim["ticket"] == "T-110":
                raise CONTROL.ControllerError("lease unavailable")
            released.append(claim["ticket"])
            claim["lease_released"] = True

        controller.release_ticket_lease = release
        controller.recover_each(claims, recovery, "release-upgrade")
        waiting = [
            CONTROL.read(path) for path in controller.events.glob("*.json")
            if CONTROL.read(path).get("event")
            == "ticket_recovery_lease_release_waiting"
        ]
        self.assertEqual(ran, ["T-110", "T-111"])
        self.assertEqual(released, ["T-111"])
        self.assertEqual(len(waiting), 1)

    def test_malformed_recovery_attempt_is_typed_claim_refusal(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = self.recovery_claim()
        claim["recovery_attempt"] = {
            "count": 1, "factory_sha": None, "input_sha256": "a" * 64,
            "outcome_sha256": "b" * 64, "phase": "settled",
            "recovery": "release-upgrade", "retry_reason": "retryable",
            "retry_status": "blocked",
        }
        controller.save_claim(claim)
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "controller claim is malformed"
        ):
            controller.load_claims()

    def test_recovery_attempt_phase_count_outcome_matrix(self) -> None:
        base = {
            "count": 0, "factory_sha": self.release.name,
            "input_sha256": "a" * 64, "outcome_sha256": "",
            "phase": "pending", "recovery": "release-upgrade",
            "retry_reason": "retryable", "retry_status": "blocked",
        }
        valid = (
            (0, "", "pending"),
            (1, "b" * 64, "pending"),
            (1, "b" * 64, "settled"),
            (3, "b" * 64, "abandoning"),
            (3, "b" * 64, "abandoned"),
        )
        invalid = (
            (1, "", "settled"),
            (3, "", "abandoning"),
            (3, "", "abandoned"),
            (0, "b" * 64, "pending"),
        )
        for count, outcome, phase in valid:
            with self.subTest(valid=(count, outcome, phase)):
                self.assertTrue(CONTROL.Controller.valid_recovery_attempt({
                    **base, "count": count, "outcome_sha256": outcome,
                    "phase": phase,
                }))
        for count, outcome, phase in invalid:
            with self.subTest(invalid=(count, outcome, phase)):
                self.assertFalse(CONTROL.Controller.valid_recovery_attempt({
                    **base, "count": count, "outcome_sha256": outcome,
                    "phase": phase,
                }))

    def test_qualification_generation_change_resets_recovery_limit(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = self.recovery_claim()
        claim["lease_released"] = True
        controller.qualification_manifest_sha256 = "a" * 64

        def fail(_items):
            raise CONTROL.ControllerError("same invariant")

        controller.recover_each([claim], fail, "release-upgrade")
        controller.recover_each([claim], fail, "release-upgrade")
        self.assertEqual(claim["recovery_attempt"]["count"], 2)
        controller.qualification_manifest_sha256 = "b" * 64
        controller.recover_each([claim], fail, "release-upgrade")
        self.assertEqual(claim["recovery_attempt"]["count"], 1)

    def test_abandoned_recoveries_do_not_reduce_live_capacity_or_cross_tickets(self) -> None:
        (self.product / "factory/PROJECT.env").write_text(
            "MAX_CONCURRENT_TICKETS=3\n", encoding="utf-8",
        )
        controller = CONTROL.Controller(self.args)
        claims = [self.recovery_claim(f"T-{number}") for number in range(110, 114)]
        for claim in claims[:2]:
            claim.update(
                blocked_reason="recovery-abandoned:release-upgrade",
                lease_released=True,
            )
            claim["recovery_attempt"] = {
                "count": 3,
                "factory_sha": self.release.name,
                "input_sha256": controller.recovery_input_sha256(
                    claim, "release-upgrade"
                ),
                "outcome_sha256": "b" * 64,
                "phase": "abandoned",
                "recovery": "release-upgrade",
                "retry_reason": "retryable",
                "retry_status": "blocked",
            }
        for claim in claims[2:]:
            claim.update(parked=False, status="running")
        ran = []
        controller.recover_each(
            claims, lambda items: ran.append(items[0]["ticket"]),
            "release-upgrade",
        )
        self.assertEqual(ran, ["T-112", "T-113"])
        self.assertEqual(sum(map(controller.consumes_capacity, claims)), 2)
        self.assertEqual(controller.capacity, 3)

    def test_inactive_lease_cleanup_preserves_active_role(self) -> None:
        controller = CONTROL.Controller(self.args)
        claims = [self.recovery_claim("T-110"), self.recovery_claim("T-111")]
        active = {"T-111"}
        controller.role_active = lambda claim: claim["ticket"] in active
        released = []
        controller.release_ticket_lease = lambda claim: (
            released.append(claim["ticket"]), claim.update(lease_released=True)
        )[-1]
        controller.release_inactive_ticket_leases(claims)
        self.assertEqual(released, ["T-110"])
        self.assertNotIn("lease_released", claims[1])
        active.clear()
        controller.release_inactive_ticket_leases(claims)
        self.assertEqual(released, ["T-110", "T-111"])

    def test_inactive_lease_refusal_does_not_block_sibling_cleanup(self) -> None:
        controller = CONTROL.Controller(self.args)
        claims = [self.recovery_claim("T-110"), self.recovery_claim("T-111")]
        released = []

        def release(claim):
            if claim["ticket"] == "T-110":
                raise CONTROL.ControllerError("lease unavailable")
            released.append(claim["ticket"])
            claim["lease_released"] = True

        controller.release_ticket_lease = release
        controller.release_inactive_ticket_leases(claims)
        controller.release_inactive_ticket_leases(claims)
        waiting = [
            CONTROL.read(path) for path in controller.events.glob("*.json")
            if CONTROL.read(path).get("event")
            == "inactive_ticket_lease_release_waiting"
        ]
        self.assertEqual(released, ["T-111"])
        self.assertEqual(len(waiting), 1)

    def test_active_role_is_never_counted_as_recovery_attempt(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = self.recovery_claim()
        controller.role_active = lambda _claim: True
        calls = []
        controller.recover_each(
            [claim], lambda _items: calls.append("recovered"),
            "release-upgrade",
        )
        self.assertEqual(calls, [])
        self.assertNotIn("recovery_attempt", claim)

    def test_detached_parked_upgrade_waits_once_without_holding_lease(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "parked/T-110"
        cell.parent.mkdir()
        subprocess.run(["git", "init", "-q", str(cell)], check=True)
        (cell / "tracked").write_text("value\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(cell), "add", "tracked"], check=True)
        subprocess.run([
            "git", "-C", str(cell), "-c", "user.name=Factory",
            "-c", "user.email=factory@example.invalid", "commit", "-qm", "base",
        ], check=True)
        subprocess.run(["git", "-C", str(cell), "checkout", "-q", "--detach"], check=True)
        (self.state / "passports").mkdir(mode=0o700)
        CONTROL.write(self.state / "passports/T-110.json", {
            "factory_sha": self.release.name,
        })
        claim = {
            "branch": "ticket/T-110", "lease": "d" * 64,
            "parked": True, "receipt": "", "role": "", "status": "blocked",
            "ticket": "T-110", "worktree": str(cell),
        }
        controller.role_active = lambda _claim: False
        controller.release_ticket_lease = lambda item: item.update(
            lease_released=True
        )

        controller.recover_upgraded_claims([claim])
        controller.recover_upgraded_claims([claim])

        self.assertTrue(claim["lease_released"])
        events = [
            CONTROL.read(path) for path in controller.events.glob("*.json")
            if CONTROL.read(path).get("event") == "release_upgrade_waiting"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reason"], "detached_worktree")

    def test_detached_parked_upgrade_recovers_after_exact_branch_reattach(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = self.recovery_claim()
        claim["blocked_reason"] = "route-migration-required"
        cell = Path(claim["worktree"])
        (self.state / "passports").mkdir(mode=0o700)
        CONTROL.write(self.state / "passports/T-110.json", {
            "factory_sha": self.release.name,
        })
        controller.marker(
            f"passport-route-migration-pending-T-110-{self.release.name}",
            {"factory_sha": self.release.name, "schema": CONTROL.EVENT_SCHEMA,
             "ticket": "T-110"},
        )
        controller.marker(
            f"passport-route-migration-complete-T-110-{self.release.name}",
            {"factory_sha": self.release.name, "schema": CONTROL.EVENT_SCHEMA,
             "ticket": "T-110"},
        )
        subprocess.run(["git", "-C", str(cell), "checkout", "-q", "--detach"], check=True)
        controller.role_active = lambda _claim: False
        controller.release_ticket_lease = lambda item: item.update(
            lease_released=True
        )
        controller.recover_upgraded_claims([claim])
        self.assertTrue(claim["lease_released"])

        subprocess.run([
            "git", "-C", str(cell), "checkout", "-q", claim["branch"],
        ], check=True)
        controller.renew = lambda _claim: None
        controller.ticket_release_current = lambda _claim: True
        controller.restore_contract_blocker = lambda item: (
            item.update(status="claimed") or True
        )
        controller.event = lambda *_args, **_kwargs: None
        controller.recover_upgraded_claims([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertNotIn("blocked_reason", claim)

    def test_successor_recovers_only_exact_expired_parked_lease(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {
            "generation": 1, "mode": "successor", "tickets": ["T-110"],
        }
        cell = self.root / "parked/T-110"
        self.initialize_parked_branch(cell, "ticket/T-110")
        claim = {
            "branch": "ticket/T-110",
            "lease": "",
            "parked": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        CONTROL.write(passports / "T-110.json", {
            "branch": claim["branch"],
            "factory_sha": self.release.name,
            "ticket": claim["ticket"],
        })
        controller.marker(
            f"passport-route-migration-pending-T-110-{self.release.name}",
            {
                "factory_sha": self.release.name,
                "schema": CONTROL.EVENT_SCHEMA,
                "ticket": "T-110",
            },
        )
        leases = self.product / "factory/.dispatch-leases"
        leases.mkdir(mode=0o700)
        now = int(time.time())
        stale = {
            "claimed_epoch": now - 901,
            "expires_epoch": now - 1,
            "lease_id": "b" * 64,
            "schema_version": 1,
            "ticket": "T-110",
        }
        sibling = {
            "claimed_epoch": now,
            "expires_epoch": now + 900,
            "lease_id": "c" * 64,
            "schema_version": 1,
            "ticket": "T-111",
        }
        CONTROL.write(leases / "T-110.json", stale)
        CONTROL.write(leases / "T-111.json", sibling)
        calls = []

        def json_call(*args, **_kwargs):
            calls.append(args)
            if args[0] == "renew":
                raise CONTROL.ControllerError("parked claim owns no lease")
            if args[0] == "release-expired":
                self.assertEqual(
                    args,
                    (
                        "release-expired", "--ticket", "T-110",
                        "--lease", stale["lease_id"],
                    ),
                )
                (leases / "T-110.json").unlink()
                return {"expired": True, "released": True, "ticket": "T-110"}
            if args[0] == "claim":
                return {
                    "lease_id": "d" * 64,
                    "schema_version": 1,
                    "ticket": "T-110",
                }
            return {}

        controller.json_call = json_call
        controller.ticket_release_current = lambda _claim: True
        controller.remote_passport_valid = lambda _claim: True
        controller.migrate_passport = lambda *_args: None
        controller.restore_contract_blocker = lambda _claim: False

        controller.recover_upgraded_claims([claim])

        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["lease"], "d" * 64)
        self.assertEqual(CONTROL.read(leases / "T-111.json"), sibling)
        self.assertFalse((leases / "T-110.json").exists())
        calls_before_restart = list(calls)
        controller.recover_upgraded_claims([claim])
        self.assertEqual(calls, calls_before_restart)

        calls.clear()
        claim.pop("parked")
        claim["lease"] = stale["lease_id"]
        CONTROL.write(leases / "T-110.json", stale)
        controller.ensure_lease(claim, "successor-cohort")
        self.assertEqual(claim["lease"], "d" * 64)
        self.assertEqual(calls, [
            ("renew", "--ticket", "T-110", "--lease", stale["lease_id"]),
            (
                "release-expired", "--ticket", "T-110",
                "--lease", stale["lease_id"],
            ),
            ("claim", "--ticket", "T-110"),
        ])

    def test_successor_maintains_only_idle_cohort_leases(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {"mode": "successor"}
        parked = {
            "lease": "a" * 64, "parked": True, "status": "claimed",
            "ticket": "T-110",
        }
        idle = {
            "lease": "b" * 64, "status": "claimed", "ticket": "T-111",
        }
        active = {
            "lease": "c" * 64, "status": "running", "ticket": "T-112",
        }
        blocked = {
            "lease": "d" * 64, "parked": True, "status": "blocked",
            "ticket": "T-113",
        }
        calls = []
        controller.role_active = lambda claim: claim is active
        controller.park_claim = lambda claim: calls.append(("park", claim["ticket"]))
        controller.ensure_lease = lambda claim, label: calls.append(
            (label, claim["ticket"])
        )

        controller.maintain_successor_leases([parked, idle, active, blocked])

        self.assertEqual(calls, [
            ("park", "T-110"), ("successor-cohort", "T-111"),
        ])

    def test_successor_expired_lease_recovery_refuses_live_or_malformed(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {
            "generation": 1, "mode": "successor", "tickets": ["T-110"],
        }
        cell = self.root / "parked/T-110"
        cell.mkdir(parents=True)
        claim = {
            "branch": "ticket/T-110", "lease": "", "parked": True,
            "priority": "normal", "publication_lease": "", "receipt": "",
            "role": "", "schema": CONTROL.CLAIM_SCHEMA, "status": "blocked",
            "ticket": "T-110", "worktree": str(cell),
        }
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        CONTROL.write(passports / "T-110.json", {
            "branch": claim["branch"], "factory_sha": self.release.name,
            "ticket": claim["ticket"],
        })
        leases = self.product / "factory/.dispatch-leases"
        leases.mkdir(mode=0o700)
        now = int(time.time())
        live = {
            "claimed_epoch": now, "expires_epoch": now + 900,
            "lease_id": "b" * 64, "schema_version": 1, "ticket": "T-110",
        }
        CONTROL.write(leases / "T-110.json", live)
        controller.ticket_release_current = lambda _claim: True
        controller.remote_passport_valid = lambda _claim: True
        controller.json_call = lambda *_args, **_kwargs: self.fail(
            "live lease must not be released"
        )
        self.assertFalse(controller.release_expired_successor_lease(claim))
        self.assertEqual(CONTROL.read(leases / "T-110.json"), live)
        live["ticket"] = "T-111"
        CONTROL.write(leases / "T-110.json", live)
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "dispatcher lease state is unsafe"
        ):
            controller.release_expired_successor_lease(claim)

    def test_factory_upgrade_preserves_failed_terminal_for_recovery(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-failed-upgrade"
        cell.mkdir()
        old_factory = "b" * 40
        receipt = "c" * 64
        claim = {
            "branch": "ticket/T-110",
            "lease": "d" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": receipt,
            "role": "builder",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        passport_path = passports / "T-110.json"
        CONTROL.write(passport_path, {"factory_sha": old_factory})
        (self.product / "factory/runs/history-failure.meta").write_text(
            "run_id=history-failure\n"
            "phase=completed\n"
            "ticket=T-110\n"
            "role=builder\n"
            "accounting_state=abandoned_conservative\n"
            "exit_status=11\n"
            "role_exit=role_exit_history_rewritten\n"
            f"kit_sha={old_factory}\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )

        def migrate(_claim, _publication):
            passport = CONTROL.read(passport_path)
            passport["factory_sha"] = self.release.name
            CONTROL.write(passport_path, passport)

        controller.ticket_release_current = lambda _claim: True
        controller.renew = lambda _claim: None
        controller.migrate_passport = migrate
        controller.restore_contract_blocker = lambda _claim: False
        controller.event = lambda *_args, **_kwargs: None

        controller.recover_upgraded_claims([claim])

        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(claim["receipt"], receipt)
        self.assertEqual(claim["role"], "builder")
        self.assertEqual(
            CONTROL.read(passport_path)["factory_sha"], self.release.name
        )

    def test_successor_upgrade_reopens_qualification_cursor_failure(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {
            "mode": "successor", "tickets": ["T-110"],
        }
        old_factory = "b" * 40
        receipt = "c" * 64
        claim = {
            "branch": "ticket/T-110",
            "lease": "d" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": receipt,
            "role": "builder",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-provider-upgrade"),
        }
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        passport_path = passports / "T-110.json"
        CONTROL.write(passport_path, {"factory_sha": old_factory})
        (self.product / "factory/runs/provider-failure.meta").write_text(
            "run_id=provider-failure\n"
            "phase=completed\n"
            "ticket=T-110\n"
            "role=builder\n"
            "route_id=cursor-gpt\n"
            "accounting_state=abandoned_conservative\n"
            "exit_status=124\n"
            "role_exit=provider_failed\n"
            f"kit_sha={old_factory}\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )

        def migrate(_claim, _publication):
            passport = CONTROL.read(passport_path)
            passport["factory_sha"] = self.release.name
            CONTROL.write(passport_path, passport)

        controller.ticket_release_current = lambda _claim: True
        controller.renew = lambda _claim: None
        controller.migrate_passport = migrate
        controller.restore_contract_blocker = lambda _claim: False
        controller.event = lambda *_args, **_kwargs: None

        controller.recover_upgraded_claims([claim])

        self.assertEqual(claim["status"], "running")
        self.assertEqual(claim["receipt"], receipt)
        self.assertEqual(claim["role"], "builder")

    def test_successor_upgrade_reopens_only_prior_release_launch_void(self) -> None:
        controller = CONTROL.Controller(self.args)
        old_factory = "b" * 40
        claims = []
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        cases = (
            ("T-110", old_factory, {}),
            ("T-111", self.release.name, {}),
            ("T-112", old_factory, {"phase": "completed"}),
            ("T-113", old_factory, {"go_issued": "1"}),
            ("T-114", old_factory, {"task_submitted": "1"}),
            ("T-115", old_factory, {"effective_cost": "10.000000"}),
            ("T-116", old_factory, {"cost_basis": "provider_reported"}),
            ("T-117", "not-a-release", {}),
        )
        for number, (ticket, kit_sha, changes) in enumerate(cases, 1):
            receipt = f"{number:064x}"
            claim = {
                "branch": f"ticket/{ticket}",
                "lease": "d" * 64,
                "priority": "normal",
                "publication_lease": "",
                "receipt": receipt,
                "role": "narrator",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "blocked",
                "ticket": ticket,
                "worktree": str(self.root / f"cell-{ticket}"),
            }
            claims.append(claim)
            CONTROL.write(
                passports / f"{ticket}.json", {"factory_sha": old_factory},
            )
            terminal = {
                "phase": "abandoned",
                "go_issued": "0",
                "task_submitted": "0",
                "effective_cost": "0",
                "cost_basis": "launch_void",
                **changes,
            }
            (self.product / f"factory/runs/{ticket}-void.meta").write_text(
                f"run_id={ticket}-void\n"
                f"phase={terminal['phase']}\n"
                f"ticket={ticket}\n"
                "role=narrator\n"
                "accounting_state=launch_void\n"
                f"go_issued={terminal['go_issued']}\n"
                f"task_submitted={terminal['task_submitted']}\n"
                f"effective_cost={terminal['effective_cost']}\n"
                f"cost_basis={terminal['cost_basis']}\n"
                "exit_status=6\n"
                "role_exit=\n"
                f"kit_sha={kit_sha}\n"
                f"transition_receipt_sha256={receipt}\n",
                encoding="utf-8",
            )
        resumable = {
            "branch": "ticket/T-118",
            "lease": "d" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-118",
            "worktree": str(self.root / "cell-T-118"),
        }
        claims.append(resumable)
        CONTROL.write(
            passports / "T-118.json", {"factory_sha": old_factory},
        )

        def migrate(claim, _publication):
            CONTROL.write(
                passports / f"{claim['ticket']}.json",
                {"factory_sha": self.release.name},
            )

        controller.ticket_release_current = lambda _claim: True
        controller.renew = lambda _claim: None
        controller.migrate_passport = migrate
        controller.restore_contract_blocker = lambda _claim: False
        controller.prior_transition_tickets.update(
            claim["ticket"] for claim in claims
        )

        controller.recover_upgraded_claims(claims)

        self.assertEqual(claims[0]["status"], "running")
        self.assertEqual(claims[0]["receipt"], f"{1:064x}")
        self.assertNotIn("T-110", controller.prior_transition_tickets)
        for claim in claims[1:-1]:
            with self.subTest(ticket=claim["ticket"]):
                self.assertEqual(claim["status"], "blocked")
                self.assertTrue(claim["receipt"])
                self.assertIn(claim["ticket"], controller.prior_transition_tickets)
        self.assertEqual(resumable["status"], "claimed")
        self.assertEqual(resumable["receipt"], "")
        self.assertNotIn("T-118", controller.prior_transition_tickets)
        self.assertTrue(controller.finish_pending_run(claims[0]))
        self.assertTrue(controller.finish_pending_run(claims[0]))
        self.assertEqual(claims[0]["status"], "claimed")
        self.assertEqual(claims[0]["receipt"], "")
        events = [
            CONTROL.read(path) for path in sorted(self.state.glob("events/*.json"))
        ]
        self.assertEqual(
            sum(
                item["event"] == "pre_go_failure_recovered_by_release_upgrade"
                for item in events
            ),
            1,
        )

    def test_successor_upgrade_reopens_candidate_scoped_budget(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {
            "mode": "successor", "tickets": ["T-110"],
        }
        cell = self.root / "parked/T-110"
        self.initialize_parked_branch(cell, "ticket/T-110")
        claim = {
            "branch": "ticket/T-110",
            "budget_sha256": "b" * 64,
            "lease": "",
            "parked": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "budget",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        passport_path = passports / "T-110.json"
        CONTROL.write(passport_path, {"factory_sha": "b" * 40})

        def json_call(*args, **_kwargs):
            if args[0] == "claim":
                return {
                    "lease_id": "c" * 64,
                    "schema_version": 1,
                    "ticket": "T-110",
                }
            raise CONTROL.ControllerError("prior lease is unavailable")

        def migrate(_claim, _publication):
            CONTROL.write(
                passport_path, {"factory_sha": self.release.name},
            )

        controller.json_call = json_call
        controller.ticket_release_current = lambda _claim: True
        controller.migrate_passport = migrate
        controller.event = lambda *_args, **_kwargs: None

        controller.recover_upgraded_claims([claim])

        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["lease"], "c" * 64)
        self.assertNotIn("budget_sha256", claim)
        claim.update(status="budget", budget_sha256="d" * 64)
        controller.recover_upgraded_claims([claim])
        self.assertEqual(claim["status"], "budget")
        self.assertEqual(claim["budget_sha256"], "d" * 64)
        controller.qualification = None
        CONTROL.write(passport_path, {"factory_sha": "b" * 40})
        controller.recover_upgraded_claims([claim])
        self.assertEqual(claim["status"], "budget")
        self.assertEqual(
            CONTROL.read(passport_path)["factory_sha"], "b" * 40,
        )

    def test_factory_upgrade_recovers_waiting_claim_before_reconciliation(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "parked/T-110"
        (cell / "factory/route-plans").mkdir(parents=True)
        (cell / "factory/tickets").mkdir()
        (cell / "factory/route-plans/T-110.json").write_text(
            json.dumps({
                "kit_sha": "b" * 40,
                "ticket": "T-110",
            }) + "\n",
            encoding="utf-8",
        )
        (cell / "factory/tickets/T-110.md").write_text(
            f"# T-110\n\nState: Building\nKit-SHA: {'b' * 40}\n",
            encoding="utf-8",
        )
        pin = cell / "factory/KIT_PIN"
        pin.write_text("b" * 40 + "\n", encoding="utf-8")
        self.initialize_parked_branch(cell, "ticket/T-110")
        claim = {
            "branch": "ticket/T-110",
            "lease": "",
            "parked": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "waiting",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        (self.state / "passports").mkdir(mode=0o700)
        passport_path = self.state / "passports/T-110.json"
        CONTROL.write(passport_path, {"factory_sha": "b" * 40})
        calls = []

        def json_call(*args, **_kwargs):
            calls.append(args)
            if args[0] == "renew":
                raise CONTROL.ControllerError("waiting lease was released")
            if args[0] == "claim":
                return {
                    "lease_id": "c" * 64,
                    "schema_version": 1,
                    "ticket": "T-110",
                }
            return {}

        def migrate(_claim, _publication):
            passport = CONTROL.read(passport_path)
            passport["factory_sha"] = self.release.name
            CONTROL.write(passport_path, passport)

        controller.json_call = json_call
        controller.migrate_passport = migrate
        controller.restore_contract_blocker = lambda _claim: False
        controller.event = lambda name, *_args, **_kwargs: calls.append((name,))

        controller.recover_upgraded_claims([claim])

        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(claim["lease"], "")
        self.assertEqual(
            CONTROL.read(passport_path)["factory_sha"], self.release.name
        )
        self.assertNotIn(("claim", "--ticket", "T-110"), calls)

        (cell / "factory/route-plans/T-110.json").write_text(
            json.dumps({
                "kit_sha": self.release.name,
                "ticket": "T-110",
            }) + "\n",
            encoding="utf-8",
        )
        (cell / "factory/tickets/T-110.md").write_text(
            f"# T-110\n\nState: Building\nKit-SHA: {self.release.name}\n",
            encoding="utf-8",
        )
        pin.write_text(self.release.name + "\n", encoding="utf-8")
        controller.recover_upgraded_claims([claim])

        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["lease"], "c" * 64)
        self.assertEqual(
            CONTROL.read(passport_path)["factory_sha"], self.release.name
        )
        self.assertIn(("upgraded_claim_recovered",), calls)
        self.assertTrue(controller.marker(
            "passport-route-migration-complete-T-110-" + self.release.name
        ))

        first_calls = list(calls)
        claim.update(lease="", status="waiting")
        controller.recover_upgraded_claims([claim])
        self.assertEqual(calls, first_calls)
        self.assertEqual(claim["status"], "waiting")

    def test_maintenance_after_stage_resolution_parks_without_provider(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        parked = self.root / "parked/T-110"
        route = parked / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "parked": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(parked),
        }
        controller.save_claim(claim)
        calls = []

        def json_call(*args, **_kwargs):
            calls.append(args)
            if args[0] == "state-machine":
                (self.product / "factory/MAINTENANCE").write_text(
                    "maintenance\n", encoding="utf-8"
                )
                return {
                    "action": "RUN",
                    "detail": "builder",
                    "receipt": "b" * 64,
                    "role": "builder",
                    "schema": "nysa.software-factory.state-machine/v1",
                    "stage": "RUN builder",
                    "status": "ok",
                    "ticket": "T-110",
                }
            return {}

        controller.json_call = json_call
        controller.finish_pending_run = lambda _claim: True
        controller.refresh_dependency_tracking = lambda _claim: True
        controller.run_role = lambda *_args: self.fail(
            "maintenance must stop before provider submission"
        )

        result = controller.reconcile_ticket_until_wait(claim)

        self.assertEqual(
            result, {"status": "maintenance", "ticket": "T-110"}
        )
        self.assertEqual(claim["status"], "waiting")
        self.assertEqual(claim["lease"], "")
        self.assertEqual(
            sum(call[0] == "state-machine" for call in calls), 1
        )
        self.assertIn(
            ("release", "--ticket", "T-110", "--lease", "a" * 64),
            calls,
        )
        events = [
            CONTROL.read(path)
            for path in sorted((self.state / "events").glob("*.json"))
        ]
        paused = next(
            item for item in events
            if item["event"] == "stage_resolution_paused"
        )
        self.assertEqual(
            paused["transition_receipt_sha256"], "b" * 64
        )

    def test_maintenance_does_not_hide_malformed_transition(self) -> None:
        controller = CONTROL.Controller(self.args)
        parked = self.root / "parked/T-110"
        route = parked / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "parked": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(parked),
        }
        controller.save_claim(claim)

        def json_call(*args, **_kwargs):
            if args[0] == "state-machine":
                (self.product / "factory/MAINTENANCE").write_text(
                    "maintenance\n", encoding="utf-8"
                )
                return {
                    "action": "GARBAGE",
                    "detail": None,
                    "receipt": "b" * 64,
                    "role": None,
                    "schema": "nysa.software-factory.state-machine/v1",
                    "stage": "GARBAGE",
                    "status": "ok",
                    "ticket": "T-110",
                }
            if args[:2] == ("publication", "withdraw"):
                return {"status": "absent"}
            return {}

        controller.json_call = json_call
        controller.finish_pending_run = lambda _claim: True
        controller.refresh_dependency_tracking = lambda _claim: True
        result = controller.reconcile_ticket_until_wait(claim)

        self.assertEqual(result["status"], "error")
        self.assertEqual(
            result["error"],
            "maintenance boundary has invalid transition evidence",
        )
        self.assertEqual(claim["status"], "blocked")
        events = [
            CONTROL.read(path)
            for path in sorted((self.state / "events").glob("*.json"))
        ]
        self.assertNotIn(
            "stage_resolution_paused", {item["event"] for item in events}
        )

    def test_repaired_failure_reclaims_only_exact_remote_passport(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        cell.mkdir()
        receipt = "b" * 64
        head = "c" * 40
        passport_digest = "d" * 64
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": receipt,
            "role": "test-author",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        (self.state / "passports").mkdir(mode=0o700)
        CONTROL.write(
            self.state / "passports/T-110.json",
            {
                "branch": claim["branch"],
                "head_sha": head,
                "passport_sha256": passport_digest,
                "publication_state": "none",
            },
        )
        (self.product / "factory/runs/failed.meta").write_text(
            "run_id=failed\n"
            "ticket=T-110\n"
            "role=test-author\n"
            "accounting_state=abandoned_conservative\n"
            "exit_status=11\n"
            "role_exit=role_exit_push_failed\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )
        calls = []

        def json_call(*args, **_kwargs):
            calls.append(args)
            if args[0] == "passport":
                return {"passport": passport_digest, "status": "ok"}
            if args[0] == "renew":
                raise CONTROL.ControllerError("failed run released its lease")
            if args[0] == "claim":
                return {
                    "lease_id": "e" * 64,
                    "schema_version": 1,
                    "ticket": "T-110",
                }
            return {}

        controller.json_call = json_call
        controller.event = lambda name, *_args, **_kwargs: calls.append((name,))
        exported = set()
        controller.terminal_already_exported = lambda _claim, terminal: (
            terminal["run_id"] in exported
        )

        def export(_claim, _state):
            terminal = controller.terminal_for_receipt(
                claim["ticket"], claim["receipt"],
            )
            exported.add(terminal["run_id"])
            calls.append(("passport", "export"))

        controller.passport = export
        controller.remote_cell_head_status = lambda _claim: (
            "pushed", head, head,
        )
        remote = CONTROL.subprocess.CompletedProcess(
            [], 0, f"{head}\trefs/heads/{claim['branch']}\n", ""
        )
        controller.remote_cell_head_status = lambda _claim: (
            "pushed", head, head,
        )
        with patch.object(
            CONTROL.subprocess, "run",
            side_effect=lambda command, **_kwargs: (
                CONTROL.subprocess.CompletedProcess(command, 0, "", "")
                if "status" in command else remote
            ),
        ):
            controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["role"], "")
        self.assertEqual(claim["lease"], "e" * 64)
        self.assertEqual(
            [call[0] for call in calls],
            [
                "passport", "passport", "renew", "claim",
                "ticket_lease_recovered",
                "push_failure_recovered",
            ],
        )

        receipt = "f" * 64
        claim.update(receipt=receipt, role="reviewer", status="blocked")
        (self.product / "factory/runs/interrupted.meta").write_text(
            "run_id=interrupted\n"
            "phase=abandoned\n"
            "ticket=T-110\n"
            "role=reviewer\n"
            "accounting_state=abandoned_conservative\n"
            "task_submitted=0\n"
            "exit_status=143\n"
            "role_exit=\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )
        calls.clear()
        with patch.object(CONTROL.subprocess, "run", return_value=remote):
            controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["role"], "")
        self.assertEqual(
            [call[0] for call in calls],
            [
                "passport", "passport", "renew", "claim",
                "ticket_lease_recovered",
                "interrupted_role_recovered",
            ],
        )
        claim.update(receipt=receipt, role="reviewer", status="blocked")
        calls.clear()
        (self.product / "factory/runs/interrupted.meta").write_text(
            (self.product / "factory/runs/interrupted.meta")
            .read_text(encoding="utf-8")
            .replace("task_submitted=0", "task_submitted=1"),
            encoding="utf-8",
        )
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(calls, [])

    def test_push_failure_migrates_authorized_rewrite_before_reclaim(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-rewrite"
        cell.mkdir()
        receipt = "b" * 64
        old_digest = "c" * 64
        new_digest = "d" * 64
        head = "e" * 40
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": receipt,
            "role": "test-author",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        (self.state / "passports").mkdir(mode=0o700)
        passport_path = self.state / "passports/T-110.json"
        CONTROL.write(
            passport_path,
            {
                "branch": claim["branch"],
                "head_sha": "f" * 40,
                "passport_sha256": old_digest,
                "publication_state": "none",
            },
        )
        (self.product / "factory/runs/failed-rewrite.meta").write_text(
            "run_id=failed-rewrite\n"
            "ticket=T-110\n"
            "role=test-author\n"
            "accounting_state=abandoned_conservative\n"
            "exit_status=11\n"
            "role_exit=role_exit_push_failed\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )
        calls = []
        migrated = False
        exported = False

        def json_call(*args, **_kwargs):
            nonlocal migrated
            calls.append(args)
            if args[:2] == ("passport", "validate"):
                return {"passport": new_digest, "status": "ok"}
            if args[:2] == ("passport", "migrate"):
                migrated = True
                CONTROL.write(
                    passport_path,
                    {
                        "branch": claim["branch"],
                        "head_sha": head,
                        "passport_sha256": new_digest,
                        "publication_state": "none",
                    },
                )
            if args[0] == "renew":
                raise CONTROL.ControllerError("failed run released its lease")
            if args[0] == "claim":
                return {
                    "lease_id": "f" * 64,
                    "schema_version": 1,
                    "ticket": "T-110",
                }
            return {}

        controller.json_call = json_call
        controller.event = lambda name, *_args, **_kwargs: calls.append((name,))
        controller.terminal_already_exported = lambda *_args: exported

        def export(_claim, _state):
            nonlocal exported
            calls.append(("passport", "export"))
            if not migrated:
                raise CONTROL.ControllerError("passport head is stale")
            exported = True

        controller.passport = export
        remote = CONTROL.subprocess.CompletedProcess(
            [], 0, f"{head}\trefs/heads/{claim['branch']}\n", ""
        )
        controller.remote_cell_head_status = lambda _claim: (
            "pushed", head, head,
        )
        with patch.object(
            CONTROL.subprocess, "run",
            side_effect=lambda command, **_kwargs: (
                CONTROL.subprocess.CompletedProcess(command, 0, "", "")
                if "status" in command else remote
            ),
        ):
            controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["lease"], "f" * 64)
        self.assertEqual(
            [call[:2] for call in calls],
            [
                ("passport", "export"),
                ("passport", "migrate"),
                ("passport", "export"),
                ("passport", "validate"),
                ("renew", "--ticket"),
                ("claim", "--ticket"),
                ("ticket_lease_recovered",),
                ("push_failure_recovered",),
            ],
        )

    def test_contract_block_recovers_authorized_accepted_push_normalization_once(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-accepted-normalization"
        (cell / "factory/tickets").mkdir(parents=True)
        receipt = "b" * 64
        (cell / "factory/tickets/T-110.md").write_text(
            "# T-110\n\nState: Blocked-Escalated\nResume-State: Building\n\n"
            f"OPERATOR RESUME: builder\nOPERATOR RESUME RECEIPT: {receipt}\n",
            encoding="utf-8",
        )
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": receipt,
            "role": "builder",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        (self.state / "passports").mkdir(mode=0o700)
        CONTROL.write(
            self.state / "passports/T-110.json",
            {
                "branch": claim["branch"],
                "head_sha": "c" * 40,
                "passport_sha256": "d" * 64,
                "publication_state": "none",
            },
        )
        (self.product / "factory/runs/contract-block.meta").write_text(
            "run_id=contract-block\n"
            "phase=completed\n"
            "ticket=T-110\n"
            "role=builder\n"
            "accounting_state=abandoned_conservative\n"
            "task_submitted=1\n"
            "exit_status=12\n"
            "role_exit=role_exit_contract_blocked\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )
        calls = []
        validations = iter((False, False, True, True))
        controller.remote_passport_valid = lambda _claim: next(validations)
        remote_heads = iter((
            ("remote_unavailable", "", ""),
            ("pushed", "c" * 40, "c" * 40),
            ("pushed", "c" * 40, "c" * 40),
        ))
        controller.remote_cell_head_status = lambda _claim: next(remote_heads)
        controller.ensure_lease = lambda *_args: calls.append(("ensure-lease",))

        def migrate(_claim, publication, expected_head=""):
            calls.append(("migrate", publication, expected_head))

        controller.migrate_passport = migrate
        controller.event = lambda name, *_args, **_kwargs: calls.append((name,))

        def json_call(*args, **_kwargs):
            calls.append(args)
            if args[:2] == ("state-machine", "repair-check"):
                return {
                    "action": "repair-check", "head": "c" * 40,
                    "role": claim["role"],
                    "schema": "nysa.software-factory.state-machine/v1",
                    "status": "ready", "ticket": "T-110",
                }
            if args[:2] == ("state-machine", "block"):
                return {"status": "blocked"}
            if args[:2] == ("state-machine", "resume"):
                return {"status": "ready"}
            return {}

        controller.json_call = json_call
        controller.recover_repaired_failures([claim])
        claim["blocked_reason"] = "route-migration-required"
        controller.ticket_release_current = lambda _claim: False
        restores = []
        controller.restore_recorded_contract_repair = (
            lambda _claim: restores.append("recorded") or False
        )
        controller.restore_contract_blocker = (
            lambda _claim: restores.append("blocker") or False
        )
        controller.recover_repaired_failures([claim])
        self.assertEqual(calls, [])
        self.assertEqual(restores, [])
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(claim["receipt"], receipt)
        claim.pop("blocked_reason")
        controller.ticket_release_current = lambda _claim: True
        controller.recover_repaired_failures([claim])
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["role"], "")
        self.assertEqual(
            calls.count(("migrate", "preserve", "c" * 40)), 1,
        )
        self.assertEqual(
            calls.count(("contract_block_passport_migrated",)), 1
        )
        self.assertEqual(
            calls.count(("contract_blocker_recovered",)), 1
        )

    def test_contract_block_refuses_invalid_context_before_migration(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-invalid-contract-context"
        (cell / "factory/tickets").mkdir(parents=True)
        receipt = "b" * 64
        base = "c" * 40
        offending = "d" * 40
        tip = "e" * 40
        ticket = cell / "factory/tickets/T-110.md"
        ticket.write_text(
            "# T-110\n\nState: Blocked-Escalated\n"
            "OPERATOR ANSWER: misplaced\n"
            f"OPERATOR ANSWER RECEIPT: {receipt}\n"
            "ROLE-ESCALATE: CONTRACT-BLOCKED\n"
            "OPERATOR RESUME: planner\n"
            f"OPERATOR RESUME RECEIPT: {receipt}\n",
            encoding="utf-8",
        )
        claim = {
            "branch": "ticket/T-110", "lease": "a" * 64,
            "publication_lease": "", "receipt": receipt, "role": "planner",
            "schema": CONTROL.CLAIM_SCHEMA, "status": "blocked",
            "ticket": "T-110", "worktree": str(cell),
        }
        sibling = {
            "branch": "ticket/T-111", "lease": "f" * 64,
            "publication_lease": "", "receipt": "", "role": "",
            "schema": CONTROL.CLAIM_SCHEMA, "status": "claimed",
            "ticket": "T-111", "worktree": str(self.root / "cell-sibling"),
        }
        sibling_before = dict(sibling)
        controller.save_claim(claim)
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        passport_path = passports / "T-110.json"
        CONTROL.write(passport_path, {
            "branch": claim["branch"], "head_sha": base,
            "passport_sha256": "1" * 64, "publication_state": "none",
        })
        passport_before = passport_path.read_bytes()
        (self.product / "factory/runs/contract-invalid.meta").write_text(
            "run_id=contract-invalid\nphase=completed\n"
            "ticket=T-110\nrole=planner\n"
            "accounting_state=abandoned_conservative\n"
            "task_submitted=1\nexit_status=12\n"
            "role_exit=role_exit_contract_blocked\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )
        controller.restore_recorded_contract_repair = lambda _claim: False
        controller.restore_contract_blocker = lambda _claim: False
        controller.role_active = lambda _claim: False
        controller.remote_passport_valid = lambda _claim: False
        controller.remote_cell_head_status = lambda _claim: (
            "pushed", tip, tip,
        )
        migrations = []
        controller.migrate_passport = lambda *_args: migrations.append("migrate")
        refusals = []
        controller.event_once = (
            lambda name, ticket_id, **details:
            refusals.append((name, ticket_id, details))
        )

        def json_call(*args, **_kwargs):
            self.assertEqual(args[:2], ("state-machine", "repair-check"))
            return {
                "offending_parent": offending,
                "reason_code": "resume_parent_not_migrated",
                "status": "error",
            }

        controller.json_call = json_call
        controller.recover_repaired_failures([claim, sibling])

        self.assertEqual(migrations, [])
        self.assertEqual(passport_path.read_bytes(), passport_before)
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(claim["receipt"], receipt)
        self.assertEqual(sibling, sibling_before)
        self.assertEqual(refusals[0][0:2], ("contract_resume_refused", "T-110"))
        self.assertEqual(
            refusals[0][2]["reason_code"], "resume_parent_not_migrated"
        )
        self.assertEqual(refusals[0][2]["offending_parent"], offending)

    def test_submission_failure_retries_only_after_release_upgrade(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-submission"
        cell.mkdir()
        receipt = "b" * 64
        run_id = "submission-unconfirmed"
        claim = {
            "branch": "ticket/T-110",
            "lease": "c" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": receipt,
            "role": "test-author",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        passport = {
            "charge_records": [{
                "role": "test-author",
                "run_id": run_id,
                "transition_receipt_sha256": receipt,
            }],
            "completed_role_evidence": [],
            "transition_receipt_sha256": receipt,
        }
        CONTROL.write(passports / "T-110.json", passport)
        manifest = self.product / f"factory/runs/{run_id}.meta"

        def write_manifest(
            kit_sha: str, *, reason: str = "", output: str | None = None
        ) -> None:
            output = output or hashlib.sha256(b"").hexdigest()
            manifest.write_text(
                f"run_id={run_id}\n"
                "phase=completed\n"
                "ticket=T-110\n"
                "role=test-author\n"
                "accounting_state=abandoned_conservative\n"
                "reserved_usd=10.00\n"
                "go_issued=1\n"
                "task_submitted=0\n"
                "turns=0\n"
                "effective_cost=10.00\n"
                "exit_status=125\n"
                "cost_basis=conservative_reservation\n"
                f"kit_sha={kit_sha}\n"
                "role_exit=provider_failed\n"
                f"terminal_reason_code={reason}\n"
                f"output_sha256={output}\n"
                "progress_events=\n"
                f"transition_receipt_sha256={receipt}\n",
                encoding="utf-8",
            )

        events = []
        controller.restore_recorded_contract_repair = lambda _claim: False
        controller.restore_contract_blocker = lambda _claim: False
        controller.role_active = lambda _claim: False
        controller.remote_passport_valid = lambda _claim: True
        controller.ensure_lease = lambda _claim, _label: None
        controller.event = (
            lambda name, *_args, **details: events.append((name, details))
        )

        write_manifest(self.release.name)
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(events, [])

        write_manifest(
            "e" * 40,
            reason="adapter_submission_unconfirmed",
            output=hashlib.sha256(b"wrapper diagnostic\n").hexdigest(),
        )
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(
            events[0][0],
            "submission_failure_recovered_by_release_upgrade",
        )

        write_manifest("e" * 40)
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["role"], "")
        self.assertEqual(
            events,
            [(
                "submission_failure_recovered_by_release_upgrade",
                {"failed_run_id": run_id},
            )],
        )
        self.assertEqual(
            CONTROL.read(passports / "T-110.json")["charge_records"],
            passport["charge_records"],
        )

        claim.update(receipt=receipt, role="test-author", status="blocked")
        events.clear()
        write_manifest("e" * 40, reason="unrelated_failure")
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(events, [])

    def test_exact_converged_builder_success_recovers_once_after_upgrade(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-converged-success"
        cell.mkdir()
        receipt = "9" * 64
        run_id = "converged-success"
        claim = {
            "branch": "ticket/T-110",
            "lease": "8" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": receipt,
            "role": "builder",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        CONTROL.write(passports / "T-110.json", {})
        manifest = self.product / f"factory/runs/{run_id}.meta"
        predecessor = "e" * 40
        if predecessor == self.release.name:
            predecessor = "f" * 40

        def write_manifest(
            role_exit: str = "", adapter: str = "cursor-openai"
        ) -> None:
            manifest.write_text(
                f"run_id={run_id}\n"
                "phase=abandoned\n"
                "ticket=T-110\n"
                "role=builder\n"
                f"adapter={adapter}\n"
                "provider_attempt_id=attempt-1\n"
                "accounting_state=abandoned_conservative\n"
                "reserved_usd=10.00\n"
                "go_issued=1\n"
                "task_submitted=1\n"
                "effective_cost=10.00\n"
                "exit_status=128\n"
                "cost_basis=conservative_reservation\n"
                f"kit_sha={predecessor}\n"
                "contract_version=1.8.0\n"
                "role_branch_before=ticket/T-110\n"
                f"role_head_before={'7' * 40}\n"
                f"role_exit={role_exit}\n"
                "terminal_reason_code=\n"
                f"output_sha256={'6' * 64}\n"
                "progress_events=\n"
                "progress_journal_sha256=\n"
                f"transition_receipt_sha256={receipt}\n",
                encoding="utf-8",
            )

        events = []
        corrections = []
        controller.restore_recorded_contract_repair = lambda _claim: False
        controller.restore_contract_blocker = lambda _claim: False
        controller.remote_passport_valid = lambda _claim: True
        controller.converged_success_exported = lambda *_args: True
        controller.ensure_lease = lambda _claim, label: events.append((label, {}))
        controller.event = (
            lambda name, *_args, **details: events.append((name, details))
        )

        def correct(_claim, terminal):
            corrections.append(terminal["run_id"])

        controller.correct_converged_success = correct
        write_manifest("unrelated")
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(corrections, [])

        write_manifest(adapter="codex")
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(corrections, [])

        write_manifest()
        controller.role_active = lambda _claim: True
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(corrections, [])

        controller.role_active = lambda _claim: False
        controller.remote_passport_valid = lambda _claim: False
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(corrections, [])

        controller.remote_passport_valid = lambda _claim: True
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["role"], "")
        self.assertEqual(corrections, [run_id])
        self.assertEqual(
            events,
            [
                ("repaired-role", {}),
                (
                    "converged_success_recovered_by_release_upgrade",
                    {"failed_run_id": run_id},
                ),
            ],
        )

        claim.update(receipt=receipt, role="builder", status="blocked")
        events.clear()
        write_manifest(adapter="cursor-anthropic")
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(corrections, [run_id, run_id])

    def test_model_identity_success_recovers_before_provider_fallback(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {"tickets": ["T-110"]}
        cell = self.root / "cell-model-identity-success"
        cell.mkdir()
        receipt = "9" * 64
        run_id = "model-identity-success"
        claim = {
            "branch": "ticket/T-110",
            "lease": "8" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": receipt,
            "role": "spec-linter",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        CONTROL.write(passports / "T-110.json", {})
        predecessor = "e" * 40
        manifest = self.product / f"factory/runs/{run_id}.meta"
        manifest.write_text(
            f"run_id={run_id}\n"
            "phase=completed\n"
            "ticket=T-110\n"
            "role=spec-linter\n"
            "adapter=cursor-anthropic\n"
            "route_id=cursor-claude-opus-5-thinking-medium\n"
            "provider_attempt_id=attempt-identity\n"
            "accounting_state=abandoned_conservative\n"
            "reserved_usd=2.00\n"
            "go_issued=1\n"
            "task_submitted=1\n"
            "effective_cost=2.00\n"
            "exit_status=9\n"
            "cost_basis=conservative_reservation\n"
            f"kit_sha={predecessor}\n"
            "role_exit=provider_failed\n"
            "role_branch_before=ticket/T-110\n"
            f"role_head_before={'7' * 40}\n"
            f"output_sha256={'6' * 64}\n"
            "progress_events=2\n"
            f"progress_journal_sha256={'5' * 64}\n"
            "terminal_reason_code=\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )
        calls = []
        controller.restore_recorded_contract_repair = lambda _claim: False
        controller.restore_contract_blocker = lambda _claim: False
        controller.role_active = lambda _claim: False
        controller.restore_model_identity_success = (
            lambda _claim, terminal: calls.append(("restore", terminal["run_id"]))
        )
        controller.finish_pending_run = lambda _claim: calls.append("fallback")
        controller.remote_passport_valid = lambda _claim: True
        controller.converged_success_exported = lambda *_args: True
        controller.ensure_lease = lambda _claim, label: calls.append(label)
        controller.event = (
            lambda name, *_args, **_details: calls.append(name)
        )

        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "claimed")

        self.assertNotIn("fallback", calls)
        self.assertIn(("restore", run_id), calls)
        self.assertIn(
            "model_identity_success_recovered_by_release_upgrade", calls
        )

        for status in ("claimed", "running"):
            with self.subTest(status=status):
                claim.update(
                    receipt=receipt, role="spec-linter", status=status
                )
                calls.clear()
                controller.recover_repaired_failures([claim])
                self.assertEqual(claim["receipt"], "")
                self.assertNotIn("fallback", calls)
                self.assertIn(("restore", run_id), calls)
                self.assertIn(
                    "model_identity_success_recovered_by_release_upgrade",
                    calls,
                )

        claim.update(receipt=receipt, role="spec-linter", status="running")
        controller.role_active = lambda _claim: True
        calls.clear()
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["receipt"], receipt)
        self.assertNotIn(("restore", run_id), calls)

        controller.role_active = lambda _claim: False
        controller.release_path = self.root / ("f" * 40)
        claim.update(receipt=receipt, role="spec-linter", status="blocked")
        claim.pop("blocked_reason", None)
        calls.clear()
        typed = []

        def refuse_restore(_claim, _terminal):
            calls.append("refused-restore")
            raise CONTROL.ControllerError(
                "token=supersecret https://user:pass@example.com"
            )

        controller.restore_model_identity_success = refuse_restore
        controller.block = lambda item, reason: item.update(
            status="blocked", blocked_reason=reason
        )
        controller.event_once = (
            lambda name, ticket, **details: typed.append((name, ticket, details))
        )
        controller.recover_repaired_failures([claim])
        self.assertEqual(calls.count("refused-restore"), 1)
        self.assertEqual(
            claim["blocked_reason"],
            "model-identity-recovery-refused:" + "f" * 40,
        )
        self.assertEqual(typed[0][0:2], ("typed_recovery_refused", "T-110"))
        self.assertEqual(typed[0][2]["recovery_kind"], "model_identity_success")
        self.assertNotIn("supersecret", typed[0][2]["reason"])
        self.assertNotIn("user:pass", typed[0][2]["reason"])

        controller.recover_repaired_failures([claim])
        self.assertEqual(calls.count("refused-restore"), 1)
        self.assertEqual(len(typed), 1)

        controller.release_path = self.root / ("d" * 40)
        controller.restore_model_identity_success = (
            lambda _claim, terminal: calls.append(("successor-restore", terminal["run_id"]))
        )
        controller.recover_repaired_failures([claim])
        self.assertIn(("successor-restore", run_id), calls)
        self.assertEqual(claim["status"], "claimed")

    def test_first_model_identity_success_observation_never_replays_provider(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {"tickets": ["T-177"]}
        claim = {
            "branch": "ticket/T-177", "lease": "8" * 64,
            "publication_lease": "", "receipt": "9" * 64,
            "role": "planner", "status": "running", "ticket": "T-177",
            "worktree": str(self.root / "cell-first-model-success"),
        }
        Path(claim["worktree"]).mkdir()
        terminal = {
            "exit_status": "9", "role_exit": "provider_failed",
            "route_id": "cursor-gpt-5.6-sol-high", "run_id": "paid-success",
            "task_submitted": "1",
        }
        calls = []
        controller.terminal_for_receipt = lambda *_args: terminal
        controller.emit_attempt_terminal = lambda *_args: None
        controller.direct_model_identity_candidate = lambda *_args: True
        controller.recover_direct_model_identity_success = (
            lambda item, *_args: (
                calls.append("recover"),
                item.update(receipt="", role="", status="claimed"),
            )
        )
        controller.json_call = lambda *_args, **_kwargs: calls.append("fallback")
        controller.passport = lambda *_args: calls.append("passport")

        self.assertTrue(controller.finish_pending_run(claim))
        self.assertEqual(calls, ["recover"])
        self.assertEqual(claim["status"], "claimed")
        self.assertFalse(controller.qualification_cohort_error.is_set())

    def test_first_model_identity_refusal_is_durably_blocked(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {"tickets": ["T-177"]}
        claim = {
            "branch": "ticket/T-177", "lease": "8" * 64,
            "publication_lease": "", "receipt": "9" * 64,
            "role": "reviewer", "status": "running", "ticket": "T-177",
            "worktree": str(self.root / "cell-first-model-refusal"),
        }
        Path(claim["worktree"]).mkdir()
        terminal = {
            "exit_status": "9", "role_exit": "provider_failed",
            "route_id": "cursor-claude-sonnet-5-thinking-high",
            "run_id": "paid-success", "task_submitted": "1",
        }
        calls = []
        controller.terminal_for_receipt = lambda *_args: terminal
        controller.emit_attempt_terminal = lambda *_args: None
        controller.direct_model_identity_candidate = lambda *_args: True
        controller.recover_direct_model_identity_success = (
            lambda *_args: (_ for _ in ()).throw(
                CONTROL.ModelIdentityEvidenceError("model evidence mismatch")
            )
        )
        controller.block = lambda item, reason: (
            calls.append(("block", reason)),
            item.update(status="blocked", blocked_reason=reason),
        )
        controller.release_ticket_lease = lambda _claim: calls.append("release")
        controller.event_once = (
            lambda name, _ticket, **details: calls.append((name, details))
        )

        self.assertFalse(controller.finish_pending_run(claim))
        self.assertEqual(
            claim["blocked_reason"],
            "model-identity-recovery-refused:" + self.release.name,
        )
        self.assertEqual(calls[1], "release")
        self.assertEqual(calls[2][0], "typed_recovery_refused")
        self.assertEqual(calls[2][1]["reason"], "model evidence mismatch")
        self.assertTrue(controller.qualification_cohort_error.is_set())

    def test_model_identity_recovery_retries_only_operational_failures(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = {"ticket": "T-177", "worktree": str(self.root / "cell")}
        terminal = {"run_id": "paid-success"}

        def response(kind):
            return subprocess.CompletedProcess(
                [], 1, json.dumps({
                    "error": "redacted", "error_kind": kind,
                    "schema": "nysa.software-factory.ticket-passport/v1",
                    "status": "error", "ticket": "T-177",
                }), "",
            )

        controller.call = lambda *_args, **_kwargs: response("operation")
        with self.assertRaises(CONTROL.ControllerError) as operational:
            controller.recover_direct_model_identity_success(
                claim, terminal, "9" * 64,
            )
        self.assertNotIsInstance(
            operational.exception, CONTROL.ModelIdentityEvidenceError,
        )

        controller.call = lambda *_args, **_kwargs: response("evidence")
        with self.assertRaises(CONTROL.ModelIdentityEvidenceError):
            controller.recover_direct_model_identity_success(
                claim, terminal, "9" * 64,
            )

        controller.call = lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 75,
            '{"reason_code":"external_unavailable","status":"wait"}\n',
            "",
        )
        with self.assertRaises(CONTROL.ExternalUnavailable):
            controller.recover_direct_model_identity_success(
                claim, terminal, "9" * 64,
            )

    def test_model_identity_restore_preserves_pushed_route_migration(self) -> None:
        controller = CONTROL.Controller(self.args)
        base = "4" * 40
        restored = "5" * 40
        reverted = "6" * 40
        run_id = "identity-run"
        claim = {
            "branch": "ticket/T-110",
            "lease": "7" * 64,
            "receipt": "8" * 64,
            "ticket": "T-110",
            "worktree": str(self.root / "cell-route-migrated"),
        }
        Path(claim["worktree"]).mkdir()
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        CONTROL.write(passports / "T-110.json", {"publication_state": "none"})

        def evidence(status: str) -> dict[str, str]:
            return {
                "input_head": "1" * 40,
                "migration_count": 2,
                "migration_head": base,
                "output_head": "2" * 40,
                "output_tree": "3" * 40,
                "recovery_base_head": base,
                "recovery_status": status,
                "restore_head": restored if status == "restored" else "",
                "revert_head": reverted,
                "run_id": run_id,
                "schema": "nysa.software-factory.ticket-passport/v1",
                "status": "ok",
                "ticket": "T-110",
            }

        wrong_schema = evidence("restore-required")
        wrong_schema["schema"] = CONTROL.SCHEMA
        controller.json_call = lambda *_args, **_kwargs: wrong_schema
        with self.assertRaisesRegex(
            CONTROL.ControllerError,
            "model identity recovery evidence is invalid",
        ):
            controller.restore_model_identity_success(
                claim, {"run_id": run_id}
            )

        responses = [evidence("restore-required"), evidence("restored")]
        controller.json_call = lambda *_args, **_kwargs: responses.pop(0)
        controller.ensure_lease = lambda *_args: None
        remote = iter([
            ("pushed", base, base),
            ("resume_commit_not_pushed", restored, base),
        ])
        controller.remote_cell_head_status = lambda _claim: next(remote)
        controller.remote_cell_head_valid = lambda _claim: True
        controller.migrate_passport = lambda *_args: {"status": "ok"}
        controller.terminal_already_exported = lambda *_args: True
        corrected = []
        controller.correct_converged_success = (
            lambda *_args: corrected.append(run_id)
        )
        commands = []

        def execute(command, **_kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(CONTROL.subprocess, "run", side_effect=execute):
            controller.restore_model_identity_success(
                claim, {"run_id": run_id}
            )

        self.assertEqual(corrected, [run_id])
        self.assertEqual(commands[0][-3:], ["revert", "--no-edit", reverted])
        self.assertEqual(
            commands[1][-2:],
            ["origin", f"{restored}:refs/heads/ticket/T-110"],
        )

    def test_successor_quarantines_legacy_protected_mutation(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-protected-mutation"
        remote = self.root / "protected-mutation.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        subprocess.run(["git", "init", "-q", str(cell)], check=True)
        for key, value in (
            ("user.email", "test@nysa.dev"), ("user.name", "Test"),
        ):
            subprocess.run(
                ["git", "-C", str(cell), "config", key, value], check=True,
            )
        ticket = cell / "factory/tickets/T-110.md"
        ticket.parent.mkdir(parents=True)
        live_controls = (
            "# T-110\n\nState: Building\n"
            "SPEC-LINT: FAIL — one\n"
            "SPEC-LINT: PASS\n"
            "SPEC-LINT: FAIL — two\n"
            "OPERATOR AUTHORIZATION: spec-linter round 3\n"
            "SPEC-LINT: FAIL — three\n"
            "OPERATOR AUTHORIZATION: spec-linter round 4\n"
            "SPEC-LINT: PASS\n"
        )
        ticket.write_text(live_controls, encoding="utf-8")
        subprocess.run(["git", "-C", str(cell), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(cell), "commit", "-qm", "input"], check=True,
        )
        subprocess.run(
            ["git", "-C", str(cell), "branch", "-M", "ticket/T-110"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(cell), "remote", "add", "origin", str(remote)],
            check=True,
        )
        subprocess.run(
            [
                "git", "-C", str(cell), "push", "-q", "-u", "origin",
                "ticket/T-110",
            ],
            check=True,
        )
        input_head = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD"], text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        ticket.write_text(
            live_controls + "OPERATOR NOTE: missing round-5 authorization\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(cell), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(cell), "commit", "-qm", "bad output"],
            check=True,
        )
        output_head = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD"], text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        receipt = "b" * 64
        run_id = "protected-mutation"
        predecessor = "e" * 40
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "receipt": receipt,
            "role": "spec-linter",
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        (self.state / "passports").mkdir(mode=0o700)
        CONTROL.write(self.state / "passports/T-110.json", {
            "branch": claim["branch"],
            "charge_records": [{
                "role": "spec-linter", "run_id": run_id,
                "transition_receipt_sha256": receipt,
            }],
            "completed_role_evidence": [],
            "factory_sha": predecessor,
            "head_sha": output_head,
            "ticket": "T-110",
            "transition_receipt_sha256": receipt,
        })
        terminal = {
            "accounting_state": "abandoned_conservative",
            "cost_basis": "conservative_reservation",
            "effective_cost": "2.00",
            "exit_status": "11",
            "go_issued": "1",
            "kit_sha": predecessor,
            "phase": "completed",
            "reserved_usd": "2.00",
            "role": "spec-linter",
            "role_branch_before": claim["branch"],
            "role_exit": "role_exit_protected_ticket_mutation",
            "role_head_before": input_head,
            "role_remote_before": input_head,
            "run_id": run_id,
            "task_submitted": "1",
        }

        same_release = dict(terminal, kit_sha=self.release.name)
        self.assertFalse(
            controller.quarantine_legacy_protected_mutation(claim, same_release)
        )
        self.assertTrue(
            controller.quarantine_legacy_protected_mutation(claim, terminal)
        )
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(cell), "rev-parse", "HEAD"], text=True,
                capture_output=True, check=True,
            ).stdout.strip(),
            input_head,
        )
        self.assertEqual(
            subprocess.run(
                [
                    "git", "-C", str(cell), "rev-parse",
                    f"refs/factory/failed-role/T-110/{run_id}",
                ],
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
            output_head,
        )
        self.assertFalse(subprocess.run(
            ["git", "-C", str(cell), "status", "--porcelain=v1", "-z"],
            capture_output=True, check=True,
        ).stdout)
        self.assertEqual(ticket.read_text(encoding="utf-8"), live_controls)

        passport_path = self.state / "passports/T-110.json"
        migrated = CONTROL.read(passport_path)
        migrated.update(factory_sha=self.release.name, head_sha=input_head)
        CONTROL.write(passport_path, migrated)
        controller.terminal_for_receipt = lambda *_args: terminal
        controller.remote_passport_valid = lambda _claim: True
        controller.restore_recorded_contract_repair = lambda _claim: False
        controller.restore_contract_blocker = lambda _claim: False
        controller.role_active = lambda _claim: False
        controller.ensure_lease = lambda *_args: None
        controller.recover_repaired_failures([claim])
        self.assertEqual(
            (claim["status"], claim["receipt"], claim["role"]),
            ("claimed", "", ""),
        )

        state_args = argparse.Namespace(workdir=cell, ticket="T-110")
        stage, loop = STATE.govern_loop(state_args, "RUN spec-linter", False)
        self.assertEqual(
            stage,
            "AWAIT-OPERATOR semantic-round authorization required; add exact "
            "line: OPERATOR AUTHORIZATION: spec-linter round 5",
        )
        self.assertEqual(loop, {
            "attempt": 4, "capped": True,
            "kind": "planner-spec-linter", "limit": 3,
        })
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir()
        route.write_text("{}\n", encoding="utf-8")
        transition = state_transition(stage, ticket="T-110")
        transition["loop"] = loop
        controller.finish_pending_run = lambda _claim: True
        controller.refresh_dependency_tracking = lambda _claim: True
        controller.ticket_merged = lambda _claim: False
        controller.run_role = lambda *_args: self.fail("provider relaunched")
        controller.json_call = lambda *args, **_kwargs: (
            transition if args[0] == "state-machine" else {"status": "absent"}
        )
        result = controller.reconcile_ticket(claim)
        self.assertEqual(result, {"status": "waiting", "ticket": "T-110"})
        self.assertEqual(
            claim["blocked_reason"],
            "semantic-round-authorization:spec-linter:5",
        )
        self.assertEqual(ticket.read_text(encoding="utf-8"), live_controls)

    def test_t198_semantic_authorization_recovery_is_exact_and_one_use(self) -> None:
        source_factory = CONTROL.T198_FACTORY_SHA
        run_id = CONTROL.T198_RUN_ID
        historical_receipt = CONTROL.T198_RECEIPT

        def fixture(
            name: str, authorization: list[str], *, dirty: bool = False,
            extra_path: bool = False, extra_ticket: bool = False,
            failures: int = 2, merge: bool = False, push: bool = True,
            route_migration: bool = False, post_auth_extra: bool = False,
            blank_separator: bool = False, wrong_route_author: bool = False,
        ):
            root = self.root / name
            cell = root / "cell"
            remote = root / "remote.git"
            state = root / "controller"
            root.mkdir()
            state.mkdir(mode=0o700)
            subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
            subprocess.run(
                ["git", "init", "-q", "-b", "ticket/T-198", str(cell)],
                check=True,
            )
            for key, value in (
                ("user.email", "test@nysa.dev"), ("user.name", "Test"),
            ):
                subprocess.run(
                    ["git", "-C", str(cell), "config", key, value], check=True,
                )
            ticket = cell / "factory/tickets/T-198.md"
            route = cell / "factory/route-plans/T-198.json"
            pin = cell / "factory/KIT_PIN"
            ticket.parent.mkdir(parents=True)
            route.parent.mkdir(parents=True)
            ticket.write_text(
                "# T-198\n\nState: Planning\nKit-SHA: " + source_factory + "\n"
                + "".join(
                    f"SPEC-LINT: FAIL — failure {number}\n"
                    for number in range(1, failures + 1)
                ),
                encoding="utf-8",
            )
            old_route_value = {
                "kit_sha": source_factory,
                "schema": "ticket-model-route-plan/v1",
                "ticket": "T-198",
            }
            old_route_raw = (CONTROL.canonical(old_route_value) + "\n").encode()
            route.write_bytes(old_route_raw)
            pin.write_text(source_factory + "\n", encoding="utf-8")
            (cell / "factory/PROJECT.env").write_text(
                "MAX_CONCURRENT_TICKETS=4\n", encoding="utf-8",
            )
            (cell / "factory/ENVELOPE.env").write_text(
                "PER_TICKET_BUDGET_USD=25.000000\n", encoding="utf-8",
            )
            (cell / ".gitignore").write_text(
                "factory/runs/\n", encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(cell), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(cell), "commit", "-qm", "input"], check=True,
            )
            subprocess.run(
                ["git", "-C", str(cell), "remote", "add", "origin", str(remote)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(cell), "push", "-q", "-u", "origin", "HEAD"],
                check=True,
            )
            input_head = subprocess.run(
                ["git", "-C", str(cell), "rev-parse", "HEAD"], text=True,
                capture_output=True, check=True,
            ).stdout.strip()
            original = ticket.read_text(encoding="utf-8")
            ticket.write_text(
                original + "ROLE-ESCALATE: authorization required\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(cell), "add", str(ticket)], check=True)
            subprocess.run(
                ["git", "-C", str(cell), "commit", "-qm", "rejected output"],
                check=True,
            )
            diagnostic = subprocess.run(
                ["git", "-C", str(cell), "rev-parse", "HEAD"], text=True,
                capture_output=True, check=True,
            ).stdout.strip()
            subprocess.run([
                "git", "-C", str(cell), "update-ref",
                f"refs/factory/failed-role/T-198/{run_id}", diagnostic,
            ], check=True)
            subprocess.run(
                ["git", "-C", str(cell), "reset", "-q", "--hard", input_head],
                check=True,
            )
            ticket.write_text(
                original + ("\n" if blank_separator else "")
                + "".join(line + "\n" for line in authorization)
                + ("operator also changed prose\n" if extra_ticket else ""),
                encoding="utf-8",
            )
            if extra_path:
                (cell / "unrelated.txt").write_text("changed\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(cell), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(cell), "commit", "-qm", "operator authorization"],
                check=True,
            )
            authorization_head = subprocess.run(
                ["git", "-C", str(cell), "rev-parse", "HEAD"], text=True,
                capture_output=True, check=True,
            ).stdout.strip()
            if post_auth_extra:
                (cell / "post-auth.txt").write_text(
                    "arbitrary descendant\n", encoding="utf-8",
                )
                subprocess.run(
                    ["git", "-C", str(cell), "add", "post-auth.txt"], check=True,
                )
                subprocess.run(
                    ["git", "-C", str(cell), "commit", "-qm", "arbitrary descendant"],
                    check=True,
                )
            if merge:
                subprocess.run(
                    ["git", "-C", str(cell), "checkout", "-qb", "side", input_head],
                    check=True,
                )
                (cell / "side.txt").write_text("side\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(cell), "add", "side.txt"], check=True)
                subprocess.run(
                    ["git", "-C", str(cell), "commit", "-qm", "side"], check=True,
                )
                subprocess.run(
                    ["git", "-C", str(cell), "checkout", "-q", "ticket/T-198"],
                    check=True,
                )
                subprocess.run(
                    ["git", "-C", str(cell), "merge", "-q", "--no-ff", "side", "-m", "merge"],
                    check=True,
                )
                authorization_head = subprocess.run(
                    ["git", "-C", str(cell), "rev-parse", "HEAD"], text=True,
                    capture_output=True, check=True,
                ).stdout.strip()
            first_target = authorization_head
            final_head = authorization_head
            old_route = hashlib.sha256(old_route_raw).hexdigest()
            new_route = old_route
            migrations = [{
                "from_factory_sha": source_factory,
                "from_head_sha": input_head,
                "from_passport_file_sha256": "1" * 64,
                "from_passport_sha256": "2" * 64,
                "from_protected_base_sha": input_head,
                "from_route_plan_sha256": old_route,
                "schema": PASSPORT.MIGRATION_SCHEMA,
                "to_factory_sha": self.release.name,
                "to_head_sha": first_target,
                "to_protected_base_sha": input_head,
                "to_route_plan_sha256": old_route,
            }]
            parent_file = "1" * 64
            parent_digest = "2" * 64
            if route_migration:
                pin.write_text(self.release.name + "\n", encoding="utf-8")
                ticket.write_text(
                    ticket.read_text(encoding="utf-8").replace(
                        "Kit-SHA: " + source_factory,
                        "Kit-SHA: " + self.release.name,
                    ),
                    encoding="utf-8",
                )
                new_route_value = {
                    "kit_sha": self.release.name,
                    "revisions": [{"body": {
                            "kind": "migration",
                            "legacy_plan_b64": base64.b64encode(
                                old_route_raw
                            ).decode(),
                            "legacy_plan_sha256": old_route,
                            "new_kit_sha": source_factory,
                            "old_kit_sha": source_factory,
                        }}, {"body": {
                            "kind": "release-migration",
                            "new_kit_sha": self.release.name,
                            "old_kit_sha": source_factory,
                        }}],
                    "schema": "ticket-model-route-journal/v2",
                    "ticket": "T-198",
                }
                new_route_raw = (
                    CONTROL.canonical(new_route_value) + "\n"
                ).encode()
                route.write_bytes(new_route_raw)
                subprocess.run(
                    [
                        "git", "-C", str(cell), "add", str(pin), str(ticket),
                        str(route),
                    ],
                    check=True,
                )
                subprocess.run(
                    [
                        "git", "-C", str(cell),
                        "-c", "user.name=" + (
                            "Other" if wrong_route_author else "Software Factory"
                        ),
                        "-c", "user.email=" + (
                            "other@example.invalid"
                            if wrong_route_author else "factory@local"
                        ),
                        "commit", "-qm", "route migration",
                    ],
                    check=True,
                )
                final_head = subprocess.run(
                    ["git", "-C", str(cell), "rev-parse", "HEAD"], text=True,
                    capture_output=True, check=True,
                ).stdout.strip()
                new_route = hashlib.sha256(new_route_raw).hexdigest()
                migrations.append({
                    "from_factory_sha": self.release.name,
                    "from_head_sha": first_target,
                    "from_passport_file_sha256": "3" * 64,
                    "from_passport_sha256": "4" * 64,
                    "from_protected_base_sha": input_head,
                    "from_route_plan_sha256": old_route,
                    "schema": PASSPORT.MIGRATION_SCHEMA,
                    "to_factory_sha": self.release.name,
                    "to_head_sha": final_head,
                    "to_protected_base_sha": input_head,
                    "to_route_plan_sha256": new_route,
                })
                parent_file, parent_digest = "3" * 64, "4" * 64
            if push:
                subprocess.run(
                    ["git", "-C", str(cell), "push", "-q", "origin", "HEAD:ticket/T-198"],
                    check=True,
                )
            if dirty:
                (cell / "dirty.txt").write_text("dirty\n", encoding="utf-8")

            key = state / "passport.key"
            key.write_bytes(b"k" * 32)
            key.chmod(0o600)
            receipt = historical_receipt
            terminal = {
                "accounting_state": "abandoned_conservative",
                "cost_basis": "conservative_reservation",
                "effective_cost": "10.00", "exit_status": "11",
                "go_issued": "1", "kit_sha": source_factory,
                "phase": "completed", "reserved_usd": "10.00",
                "role": "spec-linter", "role_branch_before": "ticket/T-198",
                "role_exit": "role_exit_protected_ticket_mutation",
                "role_head_before": input_head, "role_remote_before": input_head,
                "run_id": run_id, "task_submitted": "1", "ticket": "T-198",
                "transition_receipt_sha256": receipt,
            }
            runs = cell / "factory/runs"
            runs.mkdir(mode=0o700, exist_ok=True)
            manifest = runs / f"{run_id}.meta"
            manifest.write_text(
                "\n".join(f"{name}={value}" for name, value in terminal.items())
                + "\n",
                encoding="utf-8",
            )
            manifest_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
            passport = PASSPORT.authenticate({
                "base_history": [input_head],
                "branch": "ticket/T-198",
                "charge_records": [{
                    "contract_version": "1.8.0", "factory_sha": source_factory,
                    "head_before": input_head,
                    "manifest_sha256": manifest_digest,
                    "role": "spec-linter", "run_id": run_id,
                    "transition_receipt_sha256": receipt,
                }],
                "completed_role_evidence": [],
                "contract_version": "1.8.0",
                "current_state": "Planning",
                "factory_release_history": [
                    {"contract_version": "1.8.0", "factory_sha": source_factory},
                    {"contract_version": "1.8.0", "factory_sha": self.release.name},
                ],
                "factory_sha": self.release.name,
                "head_sha": final_head,
                "head_tree": subprocess.run(
                    ["git", "-C", str(cell), "rev-parse", "HEAD^{tree}"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip(),
                "migration_history": migrations,
                "parent_digest": parent_digest,
                "parent_file_sha256": parent_file,
                "product_origin_sha256": hashlib.sha256(
                    b"git@example.invalid:nysa/product.git"
                ).hexdigest(),
                "project": "relay",
                "protected_base_sha": input_head,
                "publication_state": "none",
                "route_plan_sha256": new_route,
                "schema": "nysa.software-factory.ticket-passport/v1",
                "ticket": "T-198",
                "ticket_blob": subprocess.run(
                    ["git", "-C", str(cell), "rev-parse", "HEAD:factory/tickets/T-198.md"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip(),
                "transition_receipt_sha256": receipt,
            }, key.read_bytes())
            (state / "passports").mkdir(mode=0o700)
            PASSPORT.write_atomic(state / "passports/T-198.json", passport)
            controller = CONTROL.Controller(argparse.Namespace(
                launcher=self.launcher, product_root=cell, project="relay",
                release_path=self.release, state_dir=state,
            ))

            def passport_call(*args, **_kwargs):
                if args[:2] != ("passport", "validate"):
                    raise AssertionError(args)
                try:
                    with patch.dict(
                        os.environ,
                        {"FACTORY_CERTIFIED_PRODUCT_ORIGIN":
                         "git@example.invalid:nysa/product.git"},
                    ):
                        valid = PASSPORT.validate(argparse.Namespace(
                            project="relay", state_dir=state, ticket="T-198",
                            workdir=cell,
                        ), key.read_bytes())
                except PASSPORT.PassportError:
                    return {"status": "error"}
                return {"passport": valid["passport_sha256"], "status": "ok"}

            controller.json_call = passport_call
            claim = {
                "blocked_reason": "role-failure", "branch": "ticket/T-198",
                "lease": "6" * 64, "priority": "normal",
                "publication_lease": "", "receipt": receipt,
                "role": "spec-linter", "schema": CONTROL.CLAIM_SCHEMA,
                "status": "blocked", "ticket": "T-198",
                "worktree": str(cell),
            }
            return controller, claim, terminal, passport

        controller, claim, terminal, original_passport = fixture(
            "semantic-valid",
            ["OPERATOR AUTHORIZATION: spec-linter round 3"],
            route_migration=True, blank_separator=True,
        )
        with patch.object(CONTROL, "validate_route"):
            self.assertTrue(
                controller.exact_semantic_authorization_recovery(claim, terminal)
            )
        controller.restore_recorded_contract_repair = lambda _claim: False
        controller.restore_contract_blocker = lambda _claim: False
        loop = {
            "attempt": 2, "capped": False,
            "kind": "planner-spec-linter", "limit": 3,
        }
        issue_args = argparse.Namespace(
            contract_version="1.8.0", factory_root=controller.product,
            factory_sha=self.release.name, lease=claim["lease"], project="relay",
            state_dir=controller.state, ticket="T-198",
            workdir=Path(claim["worktree"]),
        )
        with patch.dict(
            os.environ,
            {"FACTORY_CERTIFIED_PRODUCT_ORIGIN":
             "git@example.invalid:nysa/product.git"},
        ):
            reopened = STATE.core(
                issue_args, "RUN spec-linter", "spec-linter", loop,
            )
        reopened["parent_digest"] = claim["receipt"]
        reopened["nonce"] = "7" * 32
        reopened["receipt_sha256"] = hashlib.sha256(
            STATE.canonical(reopened)
        ).hexdigest()
        reopened["consumed"] = False
        STATE.write_atomic(controller.state / "T-198.json", reopened)
        self.assertEqual(reopened["parent_digest"], claim["receipt"])
        controller.save_claim(claim)
        r1_bytes = (controller.state / "T-198.json").read_bytes()
        outage = CONTROL.Controller(argparse.Namespace(
            launcher=self.launcher, product_root=controller.product,
            project="relay", release_path=self.release, state_dir=controller.state,
        ))
        outage.json_call = controller.json_call
        outage.restore_recorded_contract_repair = lambda _claim: False
        outage.restore_contract_blocker = lambda _claim: False
        outage.release_ticket_lease = lambda *_args: self.fail(
            "transient outage released R1 lease"
        )
        with (
            patch.object(CONTROL, "validate_route"),
            patch.object(
                outage, "remote_cell_head_status",
                return_value=("remote_unavailable", "", ""),
            ),
            patch.object(outage, "remote_passport_valid", return_value=False),
        ):
            outage.release_inactive_ticket_leases([claim])
            outage.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertNotIn("lease_released", claim)
        self.assertEqual((controller.state / "T-198.json").read_bytes(), r1_bytes)
        raced = CONTROL.Controller(argparse.Namespace(
            launcher=self.launcher, product_root=controller.product,
            project="relay", release_path=self.release, state_dir=controller.state,
        ))
        raced.json_call = controller.json_call
        raced.restore_recorded_contract_repair = lambda _claim: False
        raced.restore_contract_blocker = lambda _claim: False
        raced.release_ticket_lease = lambda *_args: self.fail(
            "remote validation race released R1 lease"
        )
        with (
            patch.object(CONTROL, "validate_route"),
            patch.object(
                raced, "remote_cell_head_status",
                return_value=("remote_unavailable", "", ""),
            ),
            patch.object(raced, "remote_passport_valid", return_value=True),
        ):
            raced.release_inactive_ticket_leases([claim])
            raced.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertNotIn("lease_released", claim)
        self.assertEqual((controller.state / "T-198.json").read_bytes(), r1_bytes)
        restarted = CONTROL.Controller(argparse.Namespace(
            launcher=self.launcher, product_root=controller.product,
            project="relay", release_path=self.release, state_dir=controller.state,
        ))
        restarted.json_call = controller.json_call
        restarted.restore_recorded_contract_repair = lambda _claim: False
        restarted.restore_contract_blocker = lambda _claim: False
        restarted.release_ticket_lease = lambda *_args: self.fail(
            "R1 lease released"
        )
        restarted.ensure_lease = lambda *_args: self.fail("R1 lease replaced")
        with patch.object(CONTROL, "validate_route"):
            restarted.release_inactive_ticket_leases([claim])
            restarted.recover_repaired_failures([claim])
        self.assertEqual((claim["status"], claim["receipt"], claim["role"]), (
            "claimed", "", "",
        ))
        self.assertNotIn("blocked_reason", claim)
        self.assertNotIn("T-198", restarted.prior_transition_tickets)
        events = [
            CONTROL.read(path) for path in restarted.events.glob("*.json")
            if CONTROL.read(path).get("event")
            == "semantic_round_authorization_recovered_by_release_upgrade"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0]["transition_receipt_sha256"],
            reopened["receipt_sha256"],
        )
        retained = restarted.authenticated_operator_passport("T-198")
        self.assertEqual(retained["charge_records"], original_passport["charge_records"])
        restarted.recover_repaired_failures([claim])
        self.assertEqual(len([
            path for path in restarted.events.glob("*.json")
            if CONTROL.read(path).get("event")
            == "semantic_round_authorization_recovered_by_release_upgrade"
        ]), 1)

        invalid, invalid_claim, invalid_terminal, _passport = fixture(
            "semantic-r1-invalid",
            ["OPERATOR AUTHORIZATION: spec-linter round 3"],
            route_migration=True,
        )
        invalid_args = argparse.Namespace(
            contract_version="1.8.0", factory_root=invalid.product,
            factory_sha=self.release.name, lease=invalid_claim["lease"],
            project="relay", state_dir=invalid.state, ticket="T-198",
            workdir=Path(invalid_claim["worktree"]),
        )
        with patch.dict(
            os.environ,
            {"FACTORY_CERTIFIED_PRODUCT_ORIGIN":
             "git@example.invalid:nysa/product.git"},
        ):
            invalid_r1 = STATE.core(
                invalid_args, "RUN spec-linter", "spec-linter", loop,
            )
        invalid_r1.update(parent_digest=historical_receipt, nonce="9" * 32)
        invalid_r1["receipt_sha256"] = hashlib.sha256(
            STATE.canonical(invalid_r1)
        ).hexdigest()
        invalid_r1["consumed"] = False
        invalid_r1_path = invalid.state / "T-198.json"
        STATE.write_atomic(invalid_r1_path, invalid_r1)
        invalid.save_claim(invalid_claim)
        (invalid.product / f"factory/runs/{run_id}.meta").write_text(
            "tampered=1\n", encoding="utf-8",
        )
        r1_bytes = invalid_r1_path.read_bytes()
        releases = []
        invalid.release_ticket_lease = lambda item: (
            releases.append(item["ticket"]), item.update(lease_released=True)
        )
        with patch.object(CONTROL, "validate_route"):
            invalid.release_inactive_ticket_leases([invalid_claim])
            invalid.recover_repaired_failures([invalid_claim])
        self.assertEqual(releases, ["T-198"])
        self.assertEqual(invalid_r1_path.read_bytes(), r1_bytes)
        self.assertEqual(invalid_claim["status"], "blocked")

        for name, transition_change in (
            ("wrong-parent", {"parent_digest": "0" * 64}),
            ("wrong-lease", {"lease_sha256": "0" * 64}),
        ):
            with self.subTest(existing_r1=name):
                guarded, guarded_claim, guarded_terminal, _passport = fixture(
                    "semantic-r1-" + name,
                    ["OPERATOR AUTHORIZATION: spec-linter round 3"],
                    route_migration=True,
                )
                guarded_args = argparse.Namespace(
                    contract_version="1.8.0", factory_root=guarded.product,
                    factory_sha=self.release.name, lease=guarded_claim["lease"],
                    project="relay", state_dir=guarded.state, ticket="T-198",
                    workdir=Path(guarded_claim["worktree"]),
                )
                with patch.dict(
                    os.environ,
                    {"FACTORY_CERTIFIED_PRODUCT_ORIGIN":
                     "git@example.invalid:nysa/product.git"},
                ):
                    guarded_r1 = STATE.core(
                        guarded_args, "RUN spec-linter", "spec-linter", loop,
                    )
                guarded_r1.update({
                    "parent_digest": historical_receipt,
                    "nonce": "5" * 32,
                    **transition_change,
                })
                guarded_r1["receipt_sha256"] = hashlib.sha256(
                    STATE.canonical(guarded_r1)
                ).hexdigest()
                guarded_r1["consumed"] = False
                guarded_path = guarded.state / "T-198.json"
                STATE.write_atomic(guarded_path, guarded_r1)
                guarded.save_claim(guarded_claim)
                guarded_bytes = guarded_path.read_bytes()
                guarded.restore_recorded_contract_repair = lambda _claim: False
                guarded.restore_contract_blocker = lambda _claim: False
                guarded.release_ticket_lease = lambda item: item.update(
                    lease_released=True,
                )
                guarded.ensure_lease = lambda *_args: self.fail(
                    "invalid existing R1 triggered a replacement lease"
                )
                with patch.object(CONTROL, "validate_route"):
                    guarded.release_inactive_ticket_leases([guarded_claim])
                    guarded.recover_repaired_failures([guarded_claim])
                self.assertTrue(guarded_claim["lease_released"])
                self.assertEqual(guarded_claim["status"], "blocked")
                self.assertEqual(guarded_path.read_bytes(), guarded_bytes)

        upgrade, upgrade_claim, upgrade_terminal, _fabricated = fixture(
            "semantic-old-passport",
            ["OPERATOR AUTHORIZATION: spec-linter round 3"],
            blank_separator=True,
        )
        upgrade_runs = upgrade.product / "factory/runs"
        upgrade_runs.mkdir(mode=0o700, exist_ok=True)
        (upgrade_runs / f"{run_id}.meta").write_text(
            "\n".join(
                f"{key}={value}" for key, value in upgrade_terminal.items()
            ) + "\n",
            encoding="utf-8",
        )
        upgrade_path = upgrade.state / "passports/T-198.json"
        current = upgrade.authenticated_operator_passport("T-198")
        input_head = upgrade_terminal["role_head_before"]
        old_passport = PASSPORT.authenticate({
            **{
                key: value for key, value in current.items()
                if key not in {
                    "authentication_sha256", "passport_sha256",
                    "parent_digest", "parent_file_sha256",
                }
            },
            "factory_release_history": [{
                "contract_version": "1.8.0", "factory_sha": source_factory,
            }],
            "factory_sha": source_factory, "head_sha": input_head,
            "head_tree": subprocess.run(
                ["git", "-C", upgrade_claim["worktree"], "rev-parse",
                 f"{input_head}^{{tree}}"],
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
            "migration_history": [],
            "ticket_blob": subprocess.run(
                ["git", "-C", upgrade_claim["worktree"], "rev-parse",
                 f"{input_head}:factory/tickets/T-198.md"],
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
        }, (upgrade.state / "passport.key").read_bytes())
        for name, changes in (
            ("migration", {"migration_history": [{}]}),
            ("release", {"factory_release_history": [
                {"contract_version": "1.8.0", "factory_sha": source_factory},
                {"contract_version": "1.8.0", "factory_sha": "0" * 40},
            ]}),
        ):
            with self.subTest(old_passport=name):
                invalid_old = PASSPORT.authenticate({
                    **{
                        key: value for key, value in old_passport.items()
                        if key not in {"authentication_sha256", "passport_sha256"}
                    },
                    **changes,
                }, (upgrade.state / "passport.key").read_bytes())
                PASSPORT.write_atomic(upgrade_path, invalid_old)
                self.assertIsNone(upgrade.exact_stranded_semantic_authorization(
                    upgrade_claim, upgrade_terminal,
                    upgrade.authenticated_operator_passport("T-198"),
                ))
        PASSPORT.write_atomic(upgrade_path, old_passport)
        sibling_paths = (
            upgrade.claim_path("T-199"),
            upgrade.state / "passports/T-199.json",
            upgrade.state / "T-199.json",
        )
        for sibling in sibling_paths:
            CONTROL.write(sibling, {"schema": "sibling", "ticket": "T-199"})
        upgrade.event("sibling_checkpoint", "T-199")
        sibling_event = next(
            path for path in upgrade.events.glob("*.json")
            if CONTROL.read(path).get("ticket") == "T-199"
        )
        sibling_bytes = {
            path: path.read_bytes() for path in (*sibling_paths, sibling_event)
        }
        migrations = 0

        def migrate_upgrade_passport() -> dict:
            nonlocal migrations
            before = upgrade.authenticated_operator_passport("T-198")
            parent_file = hashlib.sha256(upgrade_path.read_bytes()).hexdigest()
            head = subprocess.run(
                ["git", "-C", upgrade_claim["worktree"], "rev-parse", "HEAD"],
                text=True, capture_output=True, check=True,
            ).stdout.strip()
            tree = subprocess.run(
                ["git", "-C", upgrade_claim["worktree"], "rev-parse",
                 "HEAD^{tree}"],
                text=True, capture_output=True, check=True,
            ).stdout.strip()
            route_digest = hashlib.sha256(
                upgrade.route_path(upgrade_claim).read_bytes()
            ).hexdigest()
            edge = {
                "from_factory_sha": before["factory_sha"],
                "from_head_sha": before["head_sha"],
                "from_passport_file_sha256": parent_file,
                "from_passport_sha256": before["passport_sha256"],
                "from_protected_base_sha": before["protected_base_sha"],
                "from_route_plan_sha256": before["route_plan_sha256"],
                "schema": PASSPORT.MIGRATION_SCHEMA,
                "to_factory_sha": self.release.name, "to_head_sha": head,
                "to_protected_base_sha": before["protected_base_sha"],
                "to_route_plan_sha256": route_digest,
            }
            history = list(before["factory_release_history"])
            release = {
                "contract_version": "1.8.0",
                "factory_sha": self.release.name,
            }
            if release not in history:
                history.append(release)
            after = PASSPORT.authenticate({
                **{
                    key: value for key, value in before.items()
                    if key not in {
                        "authentication_sha256", "passport_sha256",
                        "parent_digest", "parent_file_sha256",
                    }
                },
                "factory_release_history": history,
                "factory_sha": self.release.name, "head_sha": head,
                "head_tree": tree,
                "migration_history": [*before["migration_history"], edge],
                "parent_digest": before["passport_sha256"],
                "parent_file_sha256": parent_file,
                "route_plan_sha256": route_digest,
                "ticket_blob": subprocess.run(
                    ["git", "-C", upgrade_claim["worktree"], "rev-parse",
                     "HEAD:factory/tickets/T-198.md"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip(),
            }, (upgrade.state / "passport.key").read_bytes())
            PASSPORT.write_atomic(upgrade_path, after)
            migrations += 1
            return after

        model_calls = []
        model_preview = {}
        model_journal = {}
        crash_before_route_push = [True]
        crash_after_route_push = [True]

        def upgrade_call(*args, **_kwargs):
            if args[:2] == ("passport", "migrate"):
                value = migrate_upgrade_passport()
                return {"passport": value["passport_sha256"], "status": "ok"}
            if args[:2] == ("passport", "validate"):
                try:
                    with patch.dict(
                        os.environ,
                        {"FACTORY_CERTIFIED_PRODUCT_ORIGIN":
                         "git@example.invalid:nysa/product.git"},
                    ):
                        value = PASSPORT.validate(argparse.Namespace(
                            project="relay", state_dir=upgrade.state,
                            ticket="T-198", workdir=Path(upgrade_claim["worktree"]),
                        ), (upgrade.state / "passport.key").read_bytes())
                except PASSPORT.PassportError:
                    return {"status": "error"}
                return {"passport": value["passport_sha256"], "status": "ok"}
            if args[0] == "renew":
                return {}
            if args[:2] == ("models", "migrate-plan"):
                prior_route = upgrade.route_path(upgrade_claim).read_bytes()
                prior_value = json.loads(prior_route)
                model_journal.clear()
                if prior_value.get("schema") == "ticket-model-route-plan/v1":
                    model_journal.update({
                        "kit_sha": self.release.name,
                        "revisions": [{"body": {
                            "kind": "migration",
                            "legacy_plan_b64": base64.b64encode(
                                prior_route
                            ).decode(),
                            "legacy_plan_sha256": hashlib.sha256(
                                prior_route
                            ).hexdigest(),
                            "new_kit_sha": source_factory,
                            "old_kit_sha": source_factory,
                        }}, {"body": {
                            "kind": "release-migration",
                            "new_kit_sha": self.release.name,
                            "old_kit_sha": source_factory,
                        }}],
                        "schema": "ticket-model-route-journal/v2",
                        "ticket": "T-198",
                    })
                else:
                    model_journal.update(prior_value)
                preview_hash = hashlib.sha256(
                    CONTROL.canonical(model_journal).encode()
                ).hexdigest()
                model_preview.clear()
                model_preview.update({
                    "journal_kit_sha": self.release.name,
                    "journal_revision_count": 2,
                    "journal_tail_sha256": "6" * 64,
                    "preview_hash": preview_hash,
                    "readiness_sha256": "7" * 64,
                    "schema": "ticket-model-route-migration-preview/v1",
                    "source_document_sha256": hashlib.sha256(
                        prior_route
                    ).hexdigest(),
                    "ticket": "T-198",
                })
                model_calls.append("plan")
                return dict(model_preview)
            if args[:2] == ("models", "migrate"):
                self.assertEqual(args[7], model_preview["preview_hash"])
                self.assertEqual(args[9], model_preview["readiness_sha256"])
                self.assertEqual(args[11], "release-upgrade")
                if not upgrade.ticket_release_current(upgrade_claim):
                    (Path(upgrade_claim["worktree"]) / "factory/KIT_PIN").write_text(
                        self.release.name + "\n", encoding="utf-8",
                    )
                    ticket = Path(upgrade_claim["worktree"]) / (
                        "factory/tickets/T-198.md"
                    )
                    ticket.write_text(
                        ticket.read_text(encoding="utf-8").replace(
                            "Kit-SHA: " + source_factory,
                            "Kit-SHA: " + self.release.name,
                        ),
                        encoding="utf-8",
                    )
                    upgrade.route_path(upgrade_claim).write_text(
                        CONTROL.canonical(model_journal) + "\n",
                        encoding="utf-8",
                    )
                    subprocess.run([
                        "git", "-C", upgrade_claim["worktree"], "add", "--",
                        "factory/KIT_PIN",
                        "factory/tickets/T-198.md",
                        "factory/route-plans/T-198.json",
                    ], check=True)
                    subprocess.run([
                        "git", "-C", upgrade_claim["worktree"],
                        "-c", "user.name=Software Factory",
                        "-c", "user.email=factory@local", "commit", "-qm",
                        "T-198: migrate model route journal",
                    ], check=True)
                commit = subprocess.run([
                    "git", "-C", upgrade_claim["worktree"], "rev-parse", "HEAD",
                ], text=True, capture_output=True, check=True).stdout.strip()
                model_calls.append("apply")
                if crash_before_route_push:
                    crash_before_route_push.pop()
                    raise CONTROL.ControllerError("migration push interrupted")
                subprocess.run([
                    "git", "-C", upgrade_claim["worktree"], "push", "-q",
                    "origin", "HEAD:ticket/T-198",
                ], check=True)
                if crash_after_route_push:
                    crash_after_route_push.pop()
                    raise CONTROL.ControllerError("migration response lost")
                return {
                    **model_preview, "approved_by": "release-upgrade",
                    "commit_sha": commit, "recovered": True,
                }
            raise AssertionError(args)

        upgrade.json_call = upgrade_call
        upgrade.ticket_release_current = CONTROL.Controller.ticket_release_current.__get__(
            upgrade
        )
        upgrade.recover_upgraded_claims([upgrade_claim])
        self.assertEqual(
            (upgrade_claim["status"], upgrade_claim["blocked_reason"], migrations),
            ("blocked", "route-migration-required", 1),
        )
        first_edge = upgrade.authenticated_operator_passport("T-198")
        self.assertEqual(first_edge["migration_history"][0]["from_head_sha"], input_head)
        self.assertEqual(first_edge["migration_history"][0]["to_head_sha"], (
            subprocess.run(
                ["git", "-C", upgrade_claim["worktree"], "rev-parse", "HEAD"],
                text=True, capture_output=True, check=True,
            ).stdout.strip()
        ))
        import_events = [
            path for path in upgrade.events.glob("*.json")
            if CONTROL.read(path).get("event")
            == "semantic_round_authorization_imported_by_release_upgrade"
        ]
        self.assertEqual(len(import_events), 1)
        import_events[0].unlink()
        upgrade_claim.update(
            blocked_reason="recovery-abandoned:release-upgrade",
            lease_released=True,
        )
        upgrade_claim["recovery_attempt"] = {
            "count": CONTROL.RECOVERY_ATTEMPT_LIMIT,
            "factory_sha": self.release.name,
            "input_sha256": "0" * 64,
            "outcome_sha256": "5" * 64,
            "phase": "abandoned", "recovery": "release-upgrade",
            "retry_reason": "route-migration-required",
            "retry_status": "blocked",
        }
        upgrade_claim["recovery_attempt"]["input_sha256"] = (
            upgrade.recovery_input_sha256(upgrade_claim, "release-upgrade")
        )
        upgrade.save_claim(upgrade_claim)
        upgrade = CONTROL.Controller(argparse.Namespace(
            launcher=self.launcher, product_root=Path(upgrade_claim["worktree"]),
            project="relay", release_path=self.release,
            state_dir=upgrade.state,
        ))
        upgrade.json_call = upgrade_call
        upgrade.release_ticket_lease = lambda item: item.update(
            lease_released=True,
        )
        with patch.object(CONTROL, "validate_route"):
            upgrade.recover_each(
                [upgrade_claim], upgrade.recover_upgraded_claims,
                "release-upgrade",
            )
        self.assertEqual(migrations, 1)
        self.assertEqual(upgrade_claim["blocked_reason"], "recovery:release-upgrade")
        self.assertEqual(model_calls, ["plan", "apply"])
        local_after_crash = subprocess.run([
            "git", "-C", upgrade_claim["worktree"], "rev-parse", "HEAD",
        ], text=True, capture_output=True, check=True).stdout.strip()
        remote_after_crash = subprocess.run([
            "git", "-C", upgrade_claim["worktree"], "ls-remote", "origin",
            "refs/heads/ticket/T-198",
        ], text=True, capture_output=True, check=True).stdout.split()[0]
        self.assertNotEqual(local_after_crash, remote_after_crash)
        self.assertEqual(remote_after_crash, first_edge["head_sha"])
        with patch.object(CONTROL, "validate_route"):
            upgrade.recover_each(
                [upgrade_claim], upgrade.recover_upgraded_claims,
                "release-upgrade",
            )
        self.assertEqual(migrations, 1)
        self.assertEqual(upgrade_claim["blocked_reason"], "recovery:release-upgrade")
        self.assertEqual(
            model_calls, ["plan", "apply", "plan", "apply"],
        )
        remote_after_push = subprocess.run([
            "git", "-C", upgrade_claim["worktree"], "ls-remote", "origin",
            "refs/heads/ticket/T-198",
        ], text=True, capture_output=True, check=True).stdout.split()[0]
        self.assertEqual(remote_after_push, local_after_crash)
        with patch.object(CONTROL, "validate_route"):
            upgrade.recover_each(
                [upgrade_claim], upgrade.recover_upgraded_claims,
                "release-upgrade",
            )
        self.assertEqual(migrations, 2)
        self.assertEqual(
            model_calls,
            ["plan", "apply", "plan", "apply", "plan", "apply"],
        )
        self.assertEqual(len([
            path for path in upgrade.events.glob("*.json")
            if CONTROL.read(path).get("event")
            == "stranded_route_upgrade_readmitted"
        ]), 1)
        self.assertEqual(len([
            path for path in upgrade.events.glob("*.json")
            if CONTROL.read(path).get("event")
            == "semantic_round_authorization_imported_by_release_upgrade"
        ]), 1)
        self.assertEqual(upgrade_claim["status"], "blocked")
        self.assertNotIn("blocked_reason", upgrade_claim)
        with patch.object(CONTROL, "validate_route"):
            self.assertTrue(upgrade.exact_semantic_authorization_recovery(
                upgrade_claim, upgrade_terminal,
            ))
        upgrade_loop = {
            "attempt": 2, "capped": False,
            "kind": "planner-spec-linter", "limit": 3,
        }
        upgrade_issue = argparse.Namespace(
            contract_version="1.8.0", factory_root=upgrade.product,
            factory_sha=self.release.name, lease=upgrade_claim["lease"],
            project="relay",
            state_dir=upgrade.state, ticket="T-198",
            workdir=Path(upgrade_claim["worktree"]),
        )
        upgraded_passport = upgrade.authenticated_operator_passport("T-198")

        def targeted_call(*args, **kwargs):
            if args[0] != "state-machine":
                return upgrade_call(*args, **kwargs)
            with patch.dict(
                os.environ,
                {"FACTORY_CERTIFIED_PRODUCT_ORIGIN":
                 "git@example.invalid:nysa/product.git"},
            ):
                issued = STATE.core(
                    upgrade_issue, "RUN spec-linter", "spec-linter",
                    upgrade_loop,
                )
            issued["parent_digest"] = upgrade_claim["receipt"]
            issued["nonce"] = "8" * 32
            issued["receipt_sha256"] = hashlib.sha256(
                STATE.canonical(issued)
            ).hexdigest()
            issued["consumed"] = False
            STATE.write_atomic(upgrade.state / "T-198.json", issued)
            result = state_transition(
                "RUN spec-linter", issued["receipt_sha256"], "T-198",
            )
            result["loop"] = upgrade_loop
            return result

        upgrade.json_call = targeted_call
        upgrade.restore_recorded_contract_repair = lambda _claim: False
        upgrade.restore_contract_blocker = lambda _claim: False
        upgrade.ensure_lease = lambda *_args: None
        with patch.object(CONTROL, "validate_route"):
            upgrade.recover_repaired_failures([upgrade_claim])
        self.assertEqual(
            (upgrade_claim["status"], upgrade_claim["receipt"], upgrade_claim["role"]),
            ("claimed", "", ""),
        )
        retained_charge = upgrade.authenticated_operator_passport("T-198")
        self.assertEqual(
            retained_charge["charge_records"], upgraded_passport["charge_records"],
        )
        persisted_r1 = CONTROL.read(upgrade.state / "T-198.json")
        self.assertEqual(persisted_r1["parent_digest"], historical_receipt)
        self.assertFalse(persisted_r1["consumed"])
        self.assertEqual(len([
            path for path in upgrade.events.glob("*.json")
            if CONTROL.read(path).get("event")
            == "semantic_round_authorization_recovered_by_release_upgrade"
        ]), 1)
        self.assertEqual(
            {path: path.read_bytes() for path in sibling_bytes}, sibling_bytes,
        )

        cases = {
            "stale": dict(authorization=[
                "OPERATOR AUTHORIZATION: spec-linter round 2"
            ], route_migration=True),
            "future": dict(authorization=[
                "OPERATOR AUTHORIZATION: spec-linter round 4"
            ], route_migration=True),
            "wrong-role": dict(authorization=[
                "OPERATOR AUTHORIZATION: reviewer round 3"
            ], route_migration=True),
            "multiple": dict(authorization=[
                "OPERATOR AUTHORIZATION: spec-linter round 3",
                "OPERATOR AUTHORIZATION: spec-linter round 3",
            ], route_migration=True),
            "overfull": dict(
                authorization=["OPERATOR AUTHORIZATION: spec-linter round 4"],
                failures=3, route_migration=True,
            ),
            "multipath": dict(
                authorization=["OPERATOR AUTHORIZATION: spec-linter round 3"],
                extra_path=True, route_migration=True,
            ),
            "branch-advanced": dict(
                authorization=["OPERATOR AUTHORIZATION: spec-linter round 3"],
                extra_ticket=True, route_migration=True,
            ),
            "merge": dict(
                authorization=["OPERATOR AUTHORIZATION: spec-linter round 3"],
                merge=True, route_migration=True,
            ),
            "dirty": dict(
                authorization=["OPERATOR AUTHORIZATION: spec-linter round 3"],
                dirty=True, route_migration=True,
            ),
            "local-only": dict(
                authorization=["OPERATOR AUTHORIZATION: spec-linter round 3"],
                push=False, route_migration=True,
            ),
            "wrong-route-author": dict(
                authorization=[
                    "OPERATOR AUTHORIZATION: spec-linter round 3"
                ],
                route_migration=True, wrong_route_author=True,
            ),
            "post-auth-descendant": dict(
                authorization=["OPERATOR AUTHORIZATION: spec-linter round 3"],
                post_auth_extra=True, route_migration=True,
            ),
        }
        for name, options in cases.items():
            with self.subTest(case=name):
                rejected = fixture("semantic-" + name, **options)
                self.assertFalse(
                    rejected[0].exact_semantic_authorization_recovery(
                        rejected[1], rejected[2],
                    )
                )

        tampered = fixture(
            "semantic-tampered-manifest",
            ["OPERATOR AUTHORIZATION: spec-linter round 3"],
            route_migration=True,
        )
        (tampered[0].product / f"factory/runs/{run_id}.meta").write_text(
            "tampered=1\n", encoding="utf-8",
        )
        self.assertFalse(tampered[0].exact_semantic_authorization_recovery(
            tampered[1], tampered[2],
        ))

        wrong_manifest = fixture(
            "semantic-wrong-manifest",
            ["OPERATOR AUTHORIZATION: spec-linter round 3"],
            route_migration=True,
        )
        passport_path = wrong_manifest[0].state / "passports/T-198.json"
        invalid_passport = wrong_manifest[0].authenticated_operator_passport(
            "T-198"
        )
        invalid_passport["charge_records"][0]["manifest_sha256"] = "0" * 64
        invalid_passport = PASSPORT.authenticate({
            key: value for key, value in invalid_passport.items()
            if key not in {"authentication_sha256", "passport_sha256"}
        }, (wrong_manifest[0].state / "passport.key").read_bytes())
        PASSPORT.write_atomic(passport_path, invalid_passport)
        self.assertFalse(
            wrong_manifest[0].exact_semantic_authorization_recovery(
                wrong_manifest[1], wrong_manifest[2],
            )
        )

        evidence = fixture(
            "semantic-evidence-cardinality",
            ["OPERATOR AUTHORIZATION: spec-linter round 3"],
            route_migration=True,
        )
        evidence_path = evidence[0].state / "passports/T-198.json"
        evidence_passport = evidence[0].authenticated_operator_passport("T-198")
        matching_completion = {
            "role": "spec-linter", "run_id": run_id,
            "transition_receipt_sha256": historical_receipt,
        }
        for name, changes in (
            ("missing-charge", {"charge_records": []}),
            ("duplicate-charge", {
                "charge_records": evidence_passport["charge_records"] * 2,
            }),
            ("completion-present", {
                "completed_role_evidence": [matching_completion],
            }),
        ):
            with self.subTest(evidence=name):
                candidate = PASSPORT.authenticate({
                    **{
                        key: value for key, value in evidence_passport.items()
                        if key not in {
                            "authentication_sha256", "passport_sha256",
                        }
                    },
                    **changes,
                }, (evidence[0].state / "passport.key").read_bytes())
                PASSPORT.write_atomic(evidence_path, candidate)
                self.assertFalse(
                    evidence[0].exact_semantic_authorization_recovery(
                        evidence[1], evidence[2],
                    )
                )

        for name, claim_delta, terminal_delta in (
            ("other-ticket", {"ticket": "T-199"}, {}),
            ("wrong-receipt", {"receipt": "0" * 64}, {}),
            ("wrong-run", {}, {"run_id": "1786262312-97244"}),
            ("wrong-source", {}, {"kit_sha": "0" * 40}),
        ):
            with self.subTest(identity=name):
                exact = fixture(
                    "semantic-" + name,
                    ["OPERATOR AUTHORIZATION: spec-linter round 3"],
                    route_migration=True,
                )
                self.assertFalse(
                    exact[0].exact_semantic_authorization_recovery(
                        {**exact[1], **claim_delta},
                        {**exact[2], **terminal_delta},
                    )
                )

        for name, target in (("missing", None), ("moved", "HEAD")):
            with self.subTest(diagnostic=name):
                exact = fixture(
                    "semantic-diagnostic-" + name,
                    ["OPERATOR AUTHORIZATION: spec-linter round 3"],
                    route_migration=True,
                )
                ref = f"refs/factory/failed-role/T-198/{run_id}"
                command = [
                    "git", "-C", exact[1]["worktree"], "update-ref", "-d", ref,
                ] if target is None else [
                    "git", "-C", exact[1]["worktree"], "update-ref", ref, target,
                ]
                subprocess.run(command, check=True)
                self.assertFalse(
                    exact[0].exact_semantic_authorization_recovery(
                        exact[1], exact[2],
                    )
                )

        for name, terminal_change in (
            ("branch", {"role_branch_before": "ticket/T-999"}),
            ("remote", {"role_remote_before": "0" * 40}),
        ):
            with self.subTest(terminal_binding=name), patch.object(
                CONTROL, "validate_route",
            ):
                self.assertFalse(
                    controller.exact_semantic_authorization_recovery(
                        {
                            **claim, "receipt": historical_receipt,
                            "role": "spec-linter", "status": "blocked",
                        },
                        {**terminal, **terminal_change},
                    )
                )

    def test_history_rewrite_retries_only_after_release_upgrade(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-history-rewrite"
        cell.mkdir()
        receipt = "b" * 64
        input_head = "c" * 40
        run_id = "history-rewritten"
        claim = {
            "branch": "ticket/T-110",
            "lease": "d" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": receipt,
            "role": "builder",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        CONTROL.write(
            passports / "T-110.json",
            {
                "branch": claim["branch"],
                "charge_records": [{
                    "role": "builder",
                    "run_id": run_id,
                    "transition_receipt_sha256": receipt,
                }],
                "completed_role_evidence": [],
                "head_sha": input_head,
                "transition_receipt_sha256": receipt,
            },
        )
        manifest = self.product / f"factory/runs/{run_id}.meta"

        def write_manifest(
            kit_sha: str, role_exit: str = "role_exit_history_rewritten"
        ) -> None:
            manifest.write_text(
                f"run_id={run_id}\n"
                "phase=completed\n"
                "ticket=T-110\n"
                "role=builder\n"
                "accounting_state=abandoned_conservative\n"
                "reserved_usd=10.00\n"
                "go_issued=1\n"
                "task_submitted=1\n"
                "effective_cost=10.00\n"
                "exit_status=11\n"
                "cost_basis=conservative_reservation\n"
                f"kit_sha={kit_sha}\n"
                f"role_exit={role_exit}\n"
                f"role_head_before={input_head}\n"
                f"transition_receipt_sha256={receipt}\n",
                encoding="utf-8",
            )

        events = []
        leases = []
        controller.restore_recorded_contract_repair = lambda _claim: False
        controller.restore_contract_blocker = lambda _claim: False
        controller.remote_passport_valid = lambda _claim: True
        controller.ensure_lease = lambda _claim, label: leases.append(label)
        controller.event = (
            lambda name, *_args, **details: events.append((name, details))
        )

        write_manifest(self.release.name)
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(events, [])
        self.assertEqual(leases, [])

        predecessor = "e" * 40
        if predecessor == self.release.name:
            predecessor = "f" * 40
        for role_exit, event in (
            (
                "role_exit_history_rewritten",
                "history_rewrite_recovered_by_release_upgrade",
            ),
            (
                "role_exit_protected_ticket_mutation",
                "protected_ticket_mutation_recovered_by_release_upgrade",
            ),
        ):
            with self.subTest(role_exit=role_exit):
                claim.update(
                    receipt=receipt, role="builder", status="blocked",
                )
                events.clear()
                leases.clear()
                write_manifest(predecessor, role_exit)
                controller.recover_repaired_failures([claim])
                self.assertEqual(claim["status"], "claimed")
                self.assertEqual(claim["receipt"], "")
                self.assertEqual(claim["role"], "")
                self.assertEqual(leases, ["repaired-role"])
                self.assertEqual(
                    events,
                    [(event, {"failed_run_id": run_id})],
                )

    def test_exact_refresh_topology_refusal_runs_attested_refresh(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        receipt = "b" * 64
        calls = []
        controller.renew = lambda *_args: calls.append("renew")
        controller.finish_pending_run = lambda *_args: True
        stage = ""

        def json_call(*args, **_kwargs):
            calls.append(args)
            if args[0] == "state-machine":
                return state_transition(stage, receipt)
            if args[:2] == ("publication", "withdraw"):
                return {"status": "absent"}
            if args[0] == "ticket-attest":
                return {"action": "refresh", "head": "c" * 40}
            raise AssertionError(args)

        controller.json_call = json_call
        controller.migrate_passport = lambda *_args: calls.append("passport")
        controller.event = lambda name, *_args, **_kwargs: calls.append(name)
        for stage in (
            "REFUSE refresh receipt was not committed directly after its merge",
            "REFUSE stale refresh receipt does not bind this branch history",
        ):
            with self.subTest(stage=stage):
                calls.clear()
                self.assertEqual(
                    controller.reconcile_ticket(claim),
                    {"status": "progressed", "ticket": "T-110"},
                )
                self.assertEqual(
                    calls,
                    [
                        "renew",
                        (
                            "state-machine", "--ticket", "T-110", "--lease",
                            "a" * 64, "--workdir", str(cell), "--json",
                        ),
                        (
                            "publication", "withdraw", "--ticket", "T-110",
                            "--json",
                        ),
                        (
                            "ticket-attest", "--ticket", "T-110", "--lease",
                            "a" * 64, "--receipt", receipt, "--workdir", str(cell),
                            "--action", "refresh", "--json",
                        ),
                        "passport",
                        "refresh_topology_repaired",
                    ],
                )

    def test_narrator_retry_exhaustion_blocks_as_typed_escalation(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        stage = (
            "ESCALATE evidence bundle remained invalid after one Narrator retry"
        )
        calls = []
        controller.ensure_lease = lambda *_args: calls.append("renew")
        controller.finish_pending_run = lambda *_args: True
        controller.refresh_dependency_tracking = lambda *_args: True

        def json_call(*args, **_kwargs):
            calls.append(args)
            if args[0] == "state-machine":
                return state_transition(stage)
            if args[:2] == ("publication", "withdraw"):
                return {"status": "absent"}
            if args[0] == "release":
                return {"status": "released"}
            raise AssertionError(args)

        events = []
        controller.json_call = json_call
        controller.event = (
            lambda name, *_args, **details: events.append((name, details))
        )
        self.assertEqual(
            controller.reconcile_ticket(claim),
            {"status": "blocked", "ticket": "T-110"},
        )
        self.assertEqual(claim["status"], "blocked")
        self.assertTrue(claim["lease_released"])
        self.assertIn(
            ("ticket_blocked", {"reason": "state-machine-escalation"}), events,
        )
        self.assertIn(
            (
                "state_machine_escalated",
                {
                    "detail": (
                        "evidence bundle remained invalid after one Narrator retry"
                    ),
                    "passport_sha256": None,
                },
            ),
            events,
        )

    def test_budget_wait_reopens_after_envelope_or_override_change(self) -> None:
        controller = CONTROL.Controller(self.args)
        leases = iter(("b" * 64, "c" * 64))
        controller.json_call = lambda *_args, **_kwargs: {
            "lease_id": next(leases),
            "schema_version": 1,
            "ticket": "T-110",
        }
        cell = self.root / "cell-1"
        cell.mkdir()
        claim = {
            "branch": "ticket/T-110",
            "budget_sha256": controller.envelope_digest(),
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "budget",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        self.assertEqual(len(controller.load_claims()), 1)
        overrides = self.product / "factory/envelope-overrides"
        overrides.mkdir()
        (overrides / "a.json").write_text("{}\n", encoding="utf-8")
        reopened = controller.load_claims()
        self.assertEqual(reopened[0]["status"], "claimed")
        self.assertEqual(reopened[0]["lease"], "b" * 64)

        claim.update(
            budget_sha256=controller.envelope_digest(),
            status="budget",
        )
        controller.save_claim(claim)
        (self.product / "factory/ENVELOPE.env").write_text(
            "PER_TICKET_BUDGET_USD=30.000000\n", encoding="utf-8"
        )
        reopened = controller.load_claims()
        self.assertEqual(reopened[0]["status"], "claimed")
        self.assertEqual(reopened[0]["lease"], "c" * 64)

    def test_failed_admission_does_not_block_existing_claims(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.protected_main_head = lambda: "f" * 40
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        Path(claim["worktree"]).mkdir()
        route = controller.route_path(claim)
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        controller.save_claim(claim)
        events = []
        worker_started = threading.Event()

        def fail_admission(_claims):
            self.assertTrue(
                worker_started.wait(2),
                "existing pinned claim did not start before admission",
            )
            raise CONTROL.ControllerError("unsafe admission")

        controller.claim_new = fail_admission
        controller.pin_routes = lambda _claims: []

        def reconcile_ticket(item):
            worker_started.set()
            return {"status": "active", "ticket": item["ticket"]}

        controller.reconcile_ticket = reconcile_ticket
        controller.event = lambda name, **fields: events.append((name, fields))

        result = controller.reconcile()

        self.assertEqual(result["active"], 1)
        self.assertEqual(result["results"], [
            {"status": "active", "ticket": "T-110"},
        ])
        self.assertIn(
            (
                "admission_blocked",
                {
                    "error": "unsafe admission",
                    "existing_claims": ["T-110"],
                    "incident_sha256": hashlib.sha256(CONTROL.canonical({
                        "error": "unsafe admission", "reason_code": "unsafe_state",
                    }).encode()).hexdigest(),
                    "reason_code": "unsafe_state",
                },
            ),
            events,
        )

    def test_readiness_refusal_does_not_block_sibling_or_repeat_in_cycle(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.protected_main_head = lambda: "f" * 40
        cell = self.root / "cell-1"
        cell.mkdir()
        refusal = {
            "error": "provider-free ticket readiness contract is not executable",
            "reason_code": "invalid_ticket_contract",
            "ticket": "T-184",
        }
        values = [
            {
                "action": "START",
                "admission_refusal": refusal,
                "branch": "ticket/T-110",
                "lease_id": "a" * 64,
                "priority": "normal",
                "ticket": "T-110",
                "worktree": str(cell),
            },
            {"action": "WAIT", "admission_refusal": refusal},
        ]

        def admission(*args, **_kwargs):
            if args[:2] == ("dispatch-plan", "--shadow"):
                return {
                    "action": "SHADOW", "admission_refusal": refusal,
                    "ticket": "T-110",
                }
            if args[:2] == ("models", "plan"):
                return self.healthy_model_plan()
            return values.pop(0) if values else {
                "action": "WAIT", "admission_refusal": refusal,
            }

        controller.json_call = admission
        controller.pin_routes = lambda _claims: []
        controller.reconcile_ticket = lambda item: {
            "status": "active", "ticket": item["ticket"],
        }

        result = controller.reconcile()

        self.assertEqual(result["active"], 1)
        self.assertEqual(result["results"], [
            {"status": "active", "ticket": "T-110"},
            {
                "error": "provider-free ticket readiness contract is not executable",
                "reason_code": "invalid_ticket_contract",
                "status": "skipped",
                "ticket": "T-184",
            },
        ])
        incident = CONTROL.read(self.state / "admission-incident.json")
        self.assertEqual(incident["count"], 1)
        self.assertEqual(incident["ticket"], "T-184")
        events = [CONTROL.read(path) for path in controller.events.glob("*.json")]
        blocked = [item for item in events if item["event"] == "admission_blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["ticket"], "T-184")

        restarted = CONTROL.Controller(self.args)
        restarted.json_call = lambda *_args, **_kwargs: {
            "action": "WAIT", "admission_refusal": refusal,
        }
        restarted.claim_new(restarted.load_claims())
        self.assertEqual(
            CONTROL.read(self.state / "admission-incident.json")["count"], 2
        )

    def test_malformed_dispatch_refusal_fails_closed(self) -> None:
        malformed = (
            {
                "error": "ticket dependencies are invalid",
                "reason_code": "invalid_ticket_contract",
                "ticket": "not-a-ticket",
            },
            {
                "error": "provider-free ticket readiness contract is not executable",
                "reason_code": "initiative_missing",
                "ticket": "T-184",
            },
            {
                "error": "unexpected readiness failure",
                "reason_code": "invalid_ticket_contract",
                "ticket": "T-184",
            },
        )
        for refusal in malformed:
            with self.subTest(refusal=refusal):
                controller = CONTROL.Controller(self.args)
                controller.json_call = lambda *_args, **_kwargs: {
                    "action": "WAIT", "admission_refusal": refusal,
                }
                with self.assertRaisesRegex(
                    CONTROL.ControllerError,
                    "dispatch admission refusal is malformed",
                ):
                    controller.claim_new([])

    def test_named_initiative_refusal_is_returned(self) -> None:
        controller = CONTROL.Controller(self.args)
        refusal = {
            "error": "ticket initiative is missing",
            "reason_code": "initiative_missing",
            "ticket": "T-184",
        }
        controller.json_call = lambda *_args, **_kwargs: {
            "action": "WAIT", "admission_refusal": refusal,
        }
        controller.pin_routes = lambda _claims: []

        result = controller.reconcile()

        self.assertEqual(result["results"], [{**refusal, "status": "skipped"}])
        self.assertEqual(
            CONTROL.read(self.state / "admission-incident.json")["ticket"],
            "T-184",
        )

    def test_identical_admission_failure_is_durable_and_deduplicated(self) -> None:
        controller = CONTROL.Controller(self.args)
        events = []
        controller.event = lambda name, **fields: events.append((name, fields))
        error = CONTROL.ControllerError(json.dumps({
            "error": "T-100: remote Building conflicts with local Review",
            "reason_code": "unsafe_state",
        }))

        controller.record_admission_failure(error, [])
        controller.record_admission_failure(error, [])

        self.assertEqual([name for name, _ in events], ["admission_blocked"])
        incident = CONTROL.read(self.state / "admission-incident.json")
        self.assertEqual(incident["count"], 2)
        self.assertIn("remote Building", incident["error"])

        controller.record_admission_failure(
            CONTROL.ControllerError("operator projection is invalid"), []
        )
        self.assertEqual(
            [name for name, _ in events],
            ["admission_blocked", "admission_blocked"],
        )

    def test_explicit_pause_and_resume_preserve_exact_passport(self) -> None:
        controller = CONTROL.Controller(self.args)
        ticket = "T-110"
        cell = self.root / "cell-pause"
        (cell / "factory/tickets").mkdir(parents=True)
        (cell / "factory/tickets/T-110.md").write_text(
            "State: Building\nResume-State: Planning\n", encoding="utf-8"
        )
        passport = {
            "branch": f"ticket/{ticket}",
            "current_state": "Building",
            "factory_sha": self.release.name,
            "head_sha": "b" * 40,
            "migration_history": [],
            "passport_sha256": "c" * 64,
            "ticket": ticket,
        }
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        CONTROL.write(passports / f"{ticket}.json", passport)
        claim = {
            "branch": f"ticket/{ticket}",
            "lease": "d" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "waiting",
            "ticket": ticket,
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        controller.worktrees_by_branch = lambda: {
            f"refs/heads/ticket/{ticket}": [str(cell)]
        }
        controller.remote_passport_valid = lambda _claim: True
        controller.park_claim = lambda _claim: False
        controller.ticket_release_current = lambda _claim: True
        calls = []

        def launcher(*args, **_kwargs):
            calls.append(args)
            if args[0] == "claim":
                return {
                    "lease_id": "e" * 64,
                    "schema_version": 1,
                    "ticket": ticket,
                }
            return {}

        controller.json_call = launcher
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "could not park a clean checkpoint"
        ):
            controller.pause_ticket(ticket, FACTORY_ISSUE)
        self.assertTrue(controller.claim_path(ticket).exists())
        self.assertFalse(controller.pause_path(ticket).exists())
        controller.park_claim = lambda _claim: True
        self.assertEqual(
            controller.pause_ticket(ticket, FACTORY_ISSUE)["status"], "paused"
        )
        self.assertFalse(controller.claim_path(ticket).exists())
        self.assertTrue(controller.pause_path(ticket).exists())
        self.assertEqual(
            CONTROL.read(controller.pause_path(ticket))["current_state"],
            "Building",
        )
        pause = CONTROL.read(controller.pause_path(ticket))
        self.assertEqual(pause["blocking_issue"], FACTORY_ISSUE)
        self.assertEqual(pause["resume_state"], "Planning")
        self.assertRegex(pause["pause_sha256"], CONTROL.DIGEST)
        self.assertEqual(
            controller.pause_ticket(ticket, FACTORY_ISSUE)["status"], "paused"
        )

        CONTROL.write(controller.pause_path(ticket), {**pause, "status": "claimed"})
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "pause intent digest is invalid"
        ):
            controller.resume_ticket(ticket, self.release.name)
        CONTROL.write(controller.pause_path(ticket), pause)

        changed = {**passport, "head_sha": "f" * 40}
        CONTROL.write(passports / f"{ticket}.json", changed)
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "does not match the passport"
        ):
            controller.resume_ticket(ticket, self.release.name)
        CONTROL.write(passports / f"{ticket}.json", passport)

        self.assertEqual(
            controller.resume_ticket(ticket, self.release.name)["status"],
            "resumed",
        )
        resumed = CONTROL.read(controller.claim_path(ticket))
        self.assertEqual(resumed["status"], "waiting")
        self.assertEqual(resumed["lease"], "e" * 64)
        self.assertFalse(controller.pause_path(ticket).exists())
        self.assertTrue(
            (self.state / "repros" / f"{ticket}-{pause['pause_sha256']}.json").exists()
        )

        resumed["status"] = "blocked"
        controller.save_claim(resumed)
        controller.pause_ticket(ticket, FACTORY_ISSUE)
        controller.ticket_release_current = lambda _claim: False
        controller.resume_ticket(ticket, self.release.name)
        self.assertEqual(
            CONTROL.read(controller.claim_path(ticket))["status"], "blocked"
        )

        controller.pause_ticket(ticket, FACTORY_ISSUE)
        controller.ticket_release_current = lambda _claim: True
        controller.resume_ticket(ticket, self.release.name)
        self.assertEqual(
            CONTROL.read(controller.claim_path(ticket))["status"], "claimed"
        )
        controller.active_run = lambda _ticket: {"run_id": "active"}
        with self.assertRaisesRegex(CONTROL.ControllerError, "idle passport"):
            controller.pause_ticket(ticket, FACTORY_ISSUE)

    def test_ticket_control_pause_resume_covers_every_inflight_state(self) -> None:
        controller = CONTROL.Controller(self.args)
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        worktrees = {}
        cases = (
            ("Ready", "claimed", None),
            ("Planning", "claimed", None),
            ("Building", "claimed", None),
            ("Review", "waiting", None),
            ("Awaiting Approval", "waiting", None),
            ("Approved", "waiting", None),
            ("Blocked-Escalated", "blocked", "Planning"),
        )
        expected_statuses = {}

        for index, (state, status, resume_state) in enumerate(cases, 301):
            ticket = f"T-{index}"
            branch = f"ticket/{ticket}"
            cell = self.root / f"ticket-control-{index}"
            ticket_path = cell / "factory/tickets" / f"{ticket}.md"
            ticket_path.parent.mkdir(parents=True)
            ticket_path.write_text(
                f"State: {state}\n"
                + (f"Resume-State: {resume_state}\n" if resume_state else ""),
                encoding="utf-8",
            )
            passport = {
                "branch": branch,
                "current_stage": "ESCALATE planner",
                "current_state": state,
                "factory_sha": self.release.name,
                "head_sha": f"{index % 16:x}" * 40,
                "migration_history": [],
                "passport_sha256": f"{(index + 1) % 16:x}" * 64,
                "ticket": ticket,
            }
            CONTROL.write(passports / f"{ticket}.json", passport)
            controller.save_claim({
                "branch": branch,
                "lease": f"{(index + 2) % 16:x}" * 64,
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": status,
                "ticket": ticket,
                "worktree": str(cell),
            })
            worktrees[f"refs/heads/{branch}"] = [str(cell)]
            expected_statuses[ticket] = status

        controller.worktrees_by_branch = lambda: worktrees
        controller.remote_passport_valid = lambda _claim: True
        controller.park_claim = lambda _claim: True
        controller.ticket_release_current = lambda _claim: True

        def launcher(*args, **_kwargs):
            if args[0] == "claim":
                ticket = args[args.index("--ticket") + 1]
                return {
                    "lease_id": hashlib.sha256(ticket.encode()).hexdigest(),
                    "schema_version": 1,
                    "ticket": ticket,
                }
            if args[0] == "release":
                return {}
            raise AssertionError(args)

        controller.json_call = launcher
        for index, (state, _status, resume_state) in enumerate(cases, 301):
            ticket = f"T-{index}"
            ticket_path = (
                self.root / f"ticket-control-{index}"
                / "factory/tickets" / f"{ticket}.md"
            )
            with self.subTest(state=state):
                self.assertEqual(
                    controller.pause_ticket(ticket, FACTORY_ISSUE)["status"],
                    "paused",
                )
                pause = CONTROL.read(controller.pause_path(ticket))
                self.assertEqual(pause["current_state"], state)
                self.assertEqual(pause["resume_state"], resume_state)
                self.assertFalse(controller.claim_path(ticket).exists())

                self.assertEqual(
                    controller.resume_ticket(ticket, self.release.name)["status"],
                    "resumed",
                )
                resumed = CONTROL.read(controller.claim_path(ticket))
                self.assertEqual(resumed["status"], expected_statuses[ticket])
                self.assertRegex(resumed["lease"], CONTROL.DIGEST)
                self.assertFalse(controller.pause_path(ticket).exists())
                self.assertEqual(
                    ticket_path.read_text(encoding="utf-8"),
                    f"State: {state}\n"
                    + (f"Resume-State: {resume_state}\n" if resume_state else ""),
                )

    def test_ticket_control_reconstructs_only_settled_contract_blocker(self) -> None:
        controller = CONTROL.Controller(self.args)
        ticket = "T-110"
        receipt = "b" * 64
        cell = self.root / "parked" / ticket
        (cell / "factory/tickets").mkdir(parents=True)
        (cell / "factory/tickets" / f"{ticket}.md").write_text(
            "State: Blocked-Escalated\nResume-State: Planning\n",
            encoding="utf-8",
        )
        passport = {
            "branch": f"ticket/{ticket}",
            "current_stage": "ESCALATE planner",
            "current_state": "Blocked-Escalated",
            "factory_sha": self.release.name,
            "head_sha": "c" * 40,
            "migration_history": [],
            "passport_sha256": "d" * 64,
            "ticket": ticket,
        }
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        CONTROL.write(passports / f"{ticket}.json", passport)
        claim = {
            "blocked_reason": "role-failure",
            "branch": f"ticket/{ticket}",
            "lease": "e" * 64,
            "lease_released": True,
            "parked": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": receipt,
            "role": "planner",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": ticket,
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        controller.worktrees_by_branch = lambda: {
            f"refs/heads/ticket/{ticket}": [str(cell)]
        }
        controller.remote_passport_valid = lambda _claim: True
        controller.ticket_release_current = lambda _claim: True
        terminal = {
            "accounting_state": "abandoned_conservative",
            "exit_status": "12",
            "role": "planner",
            "role_exit": "role_exit_contract_blocked",
            "run_id": "settled-blocker",
            "task_submitted": "1",
        }
        controller.terminal_for_receipt = lambda *_args: terminal
        controller.json_call = lambda *args, **_kwargs: (
            {"lease_id": "f" * 64, "schema_version": 1, "ticket": ticket}
            if args[0] == "claim" else {}
        )

        controller.terminal_for_receipt = lambda *_args: {
            **terminal, "role_exit": "provider_failed",
        }
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "pre-provider boundary"
        ):
            controller.pause_ticket(ticket, FACTORY_ISSUE)

        controller.terminal_for_receipt = lambda *_args: terminal
        self.assertEqual(
            controller.pause_ticket(ticket, FACTORY_ISSUE)["status"], "paused"
        )
        changed = self.product / "factory/runs/changed.meta"
        changed.write_text(f"ticket={ticket}\n", encoding="utf-8")
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "does not match the passport"
        ):
            controller.resume_ticket(ticket, self.release.name)
        changed.unlink()
        self.assertEqual(
            controller.resume_ticket(ticket, self.release.name)["status"], "resumed"
        )
        resumed = CONTROL.read(controller.claim_path(ticket))
        self.assertEqual(resumed["status"], "blocked")
        self.assertEqual((resumed["receipt"], resumed["role"]), ("", ""))

    def test_ticket_control_pauses_preprovider_missing_terminal(self) -> None:
        controller = CONTROL.Controller(self.args)
        ticket = "T-110"
        receipt = "b" * 64
        cell = self.root / "parked" / ticket
        (cell / "factory/tickets").mkdir(parents=True)
        (cell / "factory/tickets" / f"{ticket}.md").write_text(
            "State: Building\n", encoding="utf-8",
        )
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        CONTROL.write(passports / f"{ticket}.json", {
            "branch": f"ticket/{ticket}",
            "current_stage": "RUN builder",
            "current_state": "Building",
            "factory_sha": self.release.name,
            "head_sha": "c" * 40,
            "migration_history": [],
            "passport_sha256": "d" * 64,
            "ticket": ticket,
        })
        claim = {
            "blocked_reason": "missing-terminal",
            "branch": f"ticket/{ticket}",
            "lease": "",
            "parked": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": receipt,
            "role": "builder",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": ticket,
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        controller.worktrees_by_branch = lambda: {
            f"refs/heads/ticket/{ticket}": [str(cell)]
        }
        controller.remote_passport_valid = lambda _claim: True
        controller.park_claim = lambda _claim: True
        controller.role_active = lambda _claim: False
        controller.terminal_for_receipt = lambda *_args: None
        controller.ticket_release_current = lambda _claim: True
        controller.dispatcher_lease_records = lambda: {ticket: {}}
        controller.json_call = lambda *args, **_kwargs: {
            "lease_id": "e" * 64,
            "schema_version": 1,
            "ticket": ticket,
        }

        with self.assertRaisesRegex(
            CONTROL.ControllerError, "pre-provider boundary"
        ):
            controller.pause_ticket(ticket, FACTORY_ISSUE)
        controller.dispatcher_lease_records = lambda: {}
        self.assertEqual(
            controller.pause_ticket(ticket, FACTORY_ISSUE)["status"], "paused"
        )
        late = self.product / "factory/runs/late.meta"
        late.write_text(
            f"ticket={ticket}\ntransition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "does not match the passport"
        ):
            controller.resume_ticket(ticket, self.release.name)
        late.unlink()

        self.assertEqual(
            controller.resume_ticket(ticket, self.release.name)["status"],
            "resumed",
        )
        resumed = CONTROL.read(controller.claim_path(ticket))
        self.assertEqual(resumed["status"], "claimed")
        self.assertEqual((resumed["receipt"], resumed["role"]), ("", ""))

    def test_pause_resume_state_boundary_survives_restart_and_cutover(self) -> None:
        controller = CONTROL.Controller(self.args)
        ticket = "T-110"
        cell = self.root / "cell-pause-boundary"
        (cell / "factory/tickets").mkdir(parents=True)
        (cell / "factory/tickets/T-110.md").write_text(
            "State: Building\n", encoding="utf-8"
        )
        passport = {
            "branch": f"ticket/{ticket}",
            "current_state": "Building",
            "factory_sha": self.release.name,
            "head_sha": "b" * 40,
            "migration_history": [],
            "passport_sha256": "c" * 64,
            "ticket": ticket,
        }
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        worktrees = lambda: {
            f"refs/heads/ticket/{ticket}": [str(cell)]
        }
        controller.worktrees_by_branch = worktrees
        controller.remote_passport_valid = lambda _claim: True

        self.assertEqual(CONTROL.INFLIGHT_STATES, frozenset({
            "Ready", "Planning", "Building", "Review", "Awaiting Approval",
            "Approved", "Blocked-Escalated",
        }))
        CONTROL.write(passports / f"{ticket}.json", {
            **passport, "current_state": "Backlog",
        })
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "requires an in-flight passport"
        ):
            controller.pause_ticket(ticket, FACTORY_ISSUE)
        self.assertFalse(controller.pause_path(ticket).exists())
        CONTROL.write(passports / f"{ticket}.json", {
            **passport,
            "current_state": "Approved",
            "publication_state": "merged",
        })
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "requires an in-flight passport"
        ):
            controller.pause_ticket(ticket, FACTORY_ISSUE)
        self.assertFalse(controller.pause_path(ticket).exists())

        CONTROL.write(passports / f"{ticket}.json", passport)
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "Software Factory issue URL"
        ):
            controller.pause_ticket(ticket, "https://example.com/issues/253")
        controller.pause_ticket(ticket, FACTORY_ISSUE)
        controller.qualification = {"tickets": [ticket]}
        claims = []
        controller.recover_missing_passport_claims(claims)
        self.assertEqual(claims, [])

        interrupted = {
            "branch": f"ticket/{ticket}",
            "lease": "d" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": ticket,
            "worktree": str(cell),
        }
        controller.mark_reconciling(interrupted)
        interrupted["status"] = "blocked"
        controller.save_claim(interrupted)
        controller.recover_interrupted_claims([interrupted])
        self.assertEqual(interrupted["status"], "blocked")
        self.assertTrue(controller.reconciliation_marker(ticket).exists())
        controller.claim_path(ticket).unlink()

        CONTROL.write(passports / f"{ticket}.json", {
            **passport, "current_state": "Review",
        })
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "does not match the passport"
        ):
            controller.resume_ticket(ticket, self.release.name)
        CONTROL.write(passports / f"{ticket}.json", {
            **passport, "current_state": "Done",
        })
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "requires an in-flight passport"
        ):
            controller.resume_ticket(ticket, self.release.name)
        CONTROL.write(passports / f"{ticket}.json", {
            **passport, "publication_state": "merged",
        })
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "requires an in-flight passport"
        ):
            controller.resume_ticket(ticket, self.release.name)

        CONTROL.write(passports / f"{ticket}.json", passport)
        tickets = self.product / "factory/tickets"
        tickets.mkdir()
        (tickets / f"{ticket}.md").write_text(
            "State: Done\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "requires an in-flight passport"
        ):
            controller.resume_ticket(ticket, self.release.name)
        (tickets / f"{ticket}.md").unlink()
        self.assertTrue(controller.pause_path(ticket).exists())
        self.assertFalse(controller.claim_path(ticket).exists())

        with patch.object(
            controller, "ensure_lease",
            side_effect=CONTROL.ControllerError("ticket capacity is full"),
        ):
            with self.assertRaisesRegex(
                CONTROL.ControllerError, "capacity is full"
            ):
                controller.resume_ticket(ticket, self.release.name)
        self.assertTrue(controller.pause_path(ticket).exists())
        self.assertFalse(controller.claim_path(ticket).exists())

        successor = self.root / ("e" * 40)
        successor.mkdir()
        migrated = {
            **passport,
            "factory_sha": successor.name,
            "migration_history": [{
                "from_passport_sha256": passport["passport_sha256"],
            }],
            "passport_sha256": "f" * 64,
        }
        CONTROL.write(passports / f"{ticket}.json", migrated)
        successor_args = copy.copy(self.args)
        successor_args.release_path = successor
        restarted = CONTROL.Controller(successor_args)
        restarted.worktrees_by_branch = worktrees
        restarted.remote_passport_valid = lambda _claim: True
        restarted.ticket_release_current = lambda _claim: True
        restarted.json_call = lambda *args, **_kwargs: {
            "lease_id": "1" * 64,
            "schema_version": 1,
            "ticket": ticket,
        } if args[0] == "claim" else {}

        with self.assertRaisesRegex(
            CONTROL.ControllerError, "resume intent is unavailable"
        ):
            restarted.resume_ticket(ticket, self.release.name)
        self.assertEqual(
            restarted.resume_ticket(ticket, successor.name)["status"], "resumed"
        )
        resumed = CONTROL.read(restarted.claim_path(ticket))
        self.assertEqual(resumed["lease"], "1" * 64)
        self.assertEqual(resumed["status"], "claimed")
        self.assertFalse(restarted.pause_path(ticket).exists())

    def test_pause_resume_accepts_only_one_exact_route_migration_child(self) -> None:
        controller = CONTROL.Controller(self.args)
        ticket = "T-110"
        source_factory = "9" * 40
        pause_head = "b" * 40
        route_head = "d" * 40
        cell = self.root / "cell-paused-route"
        (cell / "factory/tickets").mkdir(parents=True)
        (cell / "factory/tickets/T-110.md").write_text(
            f"State: Building\nKit-SHA: {source_factory}\n", encoding="utf-8",
        )
        passport_path = self.state / "passports/T-110.json"
        passport_path.parent.mkdir(mode=0o700)
        paused_passport = {
            "branch": f"ticket/{ticket}",
            "current_stage": "RUN builder",
            "current_state": "Building",
            "factory_sha": source_factory,
            "head_sha": pause_head,
            "migration_history": [],
            "passport_sha256": "c" * 64,
            "publication_state": "none",
            "ticket": ticket,
        }
        CONTROL.write(passport_path, paused_passport)
        controller.worktrees_by_branch = lambda: {
            f"refs/heads/ticket/{ticket}": [str(cell)]
        }
        controller.remote_passport_valid = lambda _claim: True
        controller.pause_ticket(ticket, FACTORY_ISSUE)
        pause = CONTROL.read(controller.pause_path(ticket))

        controller.ticket_release_current = lambda _claim: True
        controller.remote_cell_head_status = lambda _claim: (
            "pushed", route_head, route_head,
        )
        migrations = []

        controller.exact_route_migration_commit = lambda *_args: False
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "does not match the passport"
        ):
            controller.resume_ticket(ticket, self.release.name)
        self.assertEqual(migrations, [])

        controller.exact_route_migration_commit = lambda *_args: True
        controller.remote_cell_head_status = lambda _claim: (
            "remote_unavailable", route_head, "",
        )
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "does not match the passport"
        ):
            controller.resume_ticket(ticket, self.release.name)

        migrated_passport = {
            **paused_passport,
            "factory_sha": self.release.name,
            "head_sha": route_head,
            "migration_history": [{
                "from_factory_sha": source_factory,
                "from_head_sha": pause_head,
                "from_passport_file_sha256": "e" * 64,
                "from_passport_sha256": paused_passport["passport_sha256"],
                "from_protected_base_sha": "f" * 40,
                "from_route_plan_sha256": "1" * 64,
                "schema": CONTROL.PASSPORT_MIGRATION_SCHEMA,
                "to_factory_sha": self.release.name,
                "to_head_sha": route_head,
                "to_protected_base_sha": "f" * 40,
                "to_route_plan_sha256": "2" * 64,
            }],
            "parent_digest": paused_passport["passport_sha256"],
            "parent_file_sha256": "e" * 64,
            "passport_sha256": "3" * 64,
            "protected_base_sha": "f" * 40,
            "route_plan_sha256": "2" * 64,
        }
        CONTROL.write(passport_path, migrated_passport)
        CONTROL.write(controller.pause_path(ticket), {
            **pause, "schema": "nysa.software-factory.ticket-pause/v1",
        })
        controller.remote_cell_head_status = lambda _claim: (
            "pushed", route_head, route_head,
        )
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "does not match the passport"
        ):
            controller.resume_ticket(ticket, self.release.name)

        CONTROL.write(passport_path, paused_passport)
        CONTROL.write(controller.pause_path(ticket), pause)
        controller.migrate_passport = lambda *_args, **_kwargs: {
            "status": "error"
        }
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "passport migration failed"
        ):
            controller.resume_ticket(ticket, self.release.name)
        self.assertTrue(controller.pause_path(ticket).exists())

        def migrate(_claim, publication, expected_head=""):
            self.assertEqual((publication, expected_head), ("preserve", route_head))
            migrations.append(expected_head)
            CONTROL.write(passport_path, migrated_passport)
            return {
                "passport": migrated_passport["passport_sha256"],
                "status": "ok",
            }

        lease_attempts = 0

        def ensure_lease(claim, _reason):
            nonlocal lease_attempts
            lease_attempts += 1
            if lease_attempts == 1:
                raise CONTROL.ControllerError("ticket capacity is full")
            claim["lease"] = "4" * 64

        controller.migrate_passport = migrate
        controller.ensure_lease = ensure_lease
        with self.assertRaisesRegex(CONTROL.ControllerError, "capacity is full"):
            controller.resume_ticket(ticket, self.release.name)
        self.assertEqual(migrations, [route_head])
        self.assertEqual(CONTROL.read(passport_path)["head_sha"], route_head)
        self.assertTrue(controller.pause_path(ticket).exists())
        self.assertFalse(controller.claim_path(ticket).exists())

        self.assertEqual(
            controller.resume_ticket(ticket, self.release.name)["status"],
            "resumed",
        )
        self.assertEqual(migrations, [route_head])
        self.assertEqual(
            CONTROL.read(controller.claim_path(ticket))["lease"], "4" * 64,
        )
        self.assertFalse(controller.pause_path(ticket).exists())

    def test_interrupted_receipt_free_claim_recovers_once_from_marker(self) -> None:
        controller = CONTROL.Controller(self.args)
        ticket = "T-110"
        cell = self.root / "cell-interrupted"
        subprocess.run(["git", "init", "-q", str(cell)], check=True)
        passport = {
            "branch": f"ticket/{ticket}",
            "factory_sha": self.release.name,
            "head_sha": "b" * 40,
            "passport_sha256": "c" * 64,
            "ticket": ticket,
        }
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        CONTROL.write(passports / f"{ticket}.json", passport)
        claim = {
            "branch": f"ticket/{ticket}",
            "lease": "d" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": ticket,
            "worktree": str(cell),
        }
        controller.mark_reconciling(claim)
        claim["status"] = "blocked"
        controller.save_claim(claim)
        controller.ticket_release_current = lambda _claim: True
        controller.remote_passport_valid = lambda _claim: True
        controller.json_call = lambda *_args, **_kwargs: {}

        controller.recover_interrupted_claims([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertFalse(controller.reconciliation_marker(ticket).exists())
        controller.recover_interrupted_claims([claim])
        self.assertEqual(claim["status"], "claimed")

        claim["status"] = "blocked"
        claim["blocked_reason"] = "state-machine-refusal"
        controller.save_claim(claim)
        controller.mark_reconciling({**claim, "blocked_reason": None})
        controller.recover_interrupted_claims([claim])
        self.assertEqual(claim["status"], "blocked")

        claim.pop("blocked_reason")
        claim["receipt"] = "e" * 64
        controller.recover_interrupted_claims([claim])
        self.assertEqual(claim["status"], "blocked")
        claim["receipt"] = ""

        (cell / "dirty").write_text("dirty", encoding="utf-8")
        controller.recover_interrupted_claims([claim])
        self.assertEqual(claim["status"], "blocked")
        (cell / "dirty").unlink()

        CONTROL.write(controller.pause_path(ticket), {"ticket": ticket})
        controller.recover_interrupted_claims([claim])
        self.assertEqual(claim["status"], "blocked")
        controller.pause_path(ticket).unlink()

        controller.role_active = lambda _claim: True
        controller.recover_interrupted_claims([claim])
        self.assertEqual(claim["status"], "blocked")
        controller.role_active = lambda _claim: False

        controller.ticket_release_current = lambda _claim: False
        controller.recover_interrupted_claims([claim])
        self.assertEqual(claim["status"], "blocked")
        controller.ticket_release_current = lambda _claim: True

        (self.product / "factory/runs/new-terminal.meta").write_text(
            f"ticket={ticket}\n", encoding="utf-8"
        )
        controller.recover_interrupted_claims([claim])
        self.assertEqual(claim["status"], "blocked")
        (self.product / "factory/runs/new-terminal.meta").unlink()

        controller.remote_passport_valid = lambda _claim: False
        controller.recover_interrupted_claims([claim])
        self.assertEqual(claim["status"], "blocked")
        controller.remote_passport_valid = lambda _claim: True
        controller.recover_interrupted_claims([claim])
        self.assertEqual(claim["status"], "claimed")

        claim.update(status="blocked", blocked_reason="controller-error")
        controller.mark_reconciling(claim)
        controller.recover_interrupted_claims([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertNotIn("blocked_reason", claim)
        self.assertEqual(len([
            path for path in controller.events.glob("*.json")
            if CONTROL.read(path).get("event") == "controller_error_recovered"
        ]), 1)

    def test_successor_readmits_prior_provider_failure(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {
            "generation": 1, "mode": "successor", "tickets": ["T-110"],
        }
        ticket = "T-110"
        source = "b" * 40
        intermediate = "0" * 40
        receipt_digest = "c" * 64
        input_head = "d" * 40
        current_head = "e" * 40
        handoff_head = "f" * 40
        old_route = "1" * 64
        current_route = "2" * 64
        old_base = "3" * 40
        current_base = "4" * 40
        source_file = "5" * 64
        parent_file = "6" * 64
        parent_digest = "7" * 64
        cell = self.root / "cell-prior-role"
        subprocess.run(["git", "init", "-q", str(cell)], check=True)
        claim = {
            "branch": f"ticket/{ticket}", "lease": "8" * 64,
            "priority": "normal", "publication_lease": "",
            "receipt": receipt_digest, "role": "reviewer",
            "schema": CONTROL.CLAIM_SCHEMA, "status": "running",
            "ticket": ticket, "worktree": str(cell),
            "recovery_attempt": {
                "count": 0, "factory_sha": self.release.name,
                "input_sha256": "9" * 64, "outcome_sha256": "",
                "phase": "pending", "recovery": "release-upgrade",
                "retry_reason": "route-migration-required",
                "retry_status": "blocked",
            },
        }
        receipt = {
            "branch": claim["branch"], "consumed": True,
            "contract_version": "2.0.0", "factory_sha": source,
            "head_sha": input_head, "passport_sha256": source_file,
            "project": "relay", "receipt_sha256": receipt_digest,
            "role": "reviewer", "route_plan_sha256": old_route,
            "schema": "nysa.software-factory.transition-receipt/v1",
            "stage": "RUN reviewer", "ticket": ticket,
        }
        migrations = [{
            "from_factory_sha": source, "from_head_sha": input_head,
            "from_passport_file_sha256": source_file,
            "from_passport_sha256": "a" * 64,
            "from_protected_base_sha": old_base,
            "from_route_plan_sha256": old_route,
            "schema": CONTROL.PASSPORT_MIGRATION_SCHEMA,
            "to_factory_sha": intermediate,
            "to_head_sha": handoff_head,
            "to_protected_base_sha": current_base,
            "to_route_plan_sha256": old_route,
        }, {
            "from_factory_sha": intermediate,
            "from_head_sha": handoff_head,
            "from_passport_file_sha256": "a" * 64,
            "from_passport_sha256": "b" * 64,
            "from_protected_base_sha": current_base,
            "from_route_plan_sha256": old_route,
            "schema": CONTROL.PASSPORT_MIGRATION_SCHEMA,
            "to_factory_sha": self.release.name,
            "to_head_sha": handoff_head,
            "to_protected_base_sha": current_base,
            "to_route_plan_sha256": old_route,
        }, {
            "from_factory_sha": self.release.name,
            "from_head_sha": handoff_head,
            "from_passport_file_sha256": parent_file,
            "from_passport_sha256": parent_digest,
            "from_protected_base_sha": current_base,
            "from_route_plan_sha256": old_route,
            "schema": CONTROL.PASSPORT_MIGRATION_SCHEMA,
            "to_factory_sha": self.release.name,
            "to_head_sha": current_head,
            "to_protected_base_sha": current_base,
            "to_route_plan_sha256": current_route,
        }]
        passport = {
            "branch": claim["branch"], "current_stage": "FIX builder",
            "current_state": "Review",
            "factory_release_history": [
                {"contract_version": "2.0.0", "factory_sha": source},
                {"contract_version": "2.0.0", "factory_sha": intermediate},
                {"contract_version": "2.0.0",
                 "factory_sha": self.release.name},
            ],
            "factory_sha": self.release.name, "head_sha": current_head,
            "migration_history": migrations, "parent_digest": parent_digest,
            "parent_file_sha256": parent_file,
            "passport_sha256": "b" * 64, "protected_base_sha": current_base,
            "publication_state": "validating",
            "route_plan_sha256": current_route, "ticket": ticket,
        }
        passport_path = self.state / f"passports/{ticket}.json"
        passport_path.parent.mkdir(mode=0o700)
        CONTROL.write(passport_path, passport)
        controller.prior_transition_tickets.add(ticket)
        controller.transition_receipt = lambda *_args, **_kwargs: receipt
        controller.authenticated_operator_passport = lambda _ticket: passport
        terminal = {
            "accounting_state": "abandoned_conservative",
            "exit_status": "9", "kit_sha": source, "role": "reviewer",
            "role_exit": "provider_failed", "role_head_before": input_head,
            "route_id": "cursor-opus-v1", "run_id": "failed-reviewer",
            "task_submitted": "1",
            "transition_receipt_sha256": receipt_digest,
        }
        controller.terminal_for_receipt = lambda *_args: terminal
        controller.ticket_release_current = lambda _claim: True
        controller.remote_passport_valid = lambda _claim: True
        controller.exact_route_migration_commit = (
            lambda _claim, before, after: (before, after)
            == (handoff_head, current_head)
        )
        handoffs = []
        with patch.object(
            CONTROL, "validate_committed_output",
            side_effect=lambda *args, **kwargs: handoffs.append((args, kwargs)),
        ):
            environment = {
                "FACTORY_KIT_TRUST_SCOPE": "qualification-candidate",
                "FACTORY_QUALIFICATION_MODE": "isolated",
            }

            with patch.dict(os.environ, environment):
                self.assertTrue(
                    controller.route_migrated_failed_role(
                        claim, terminal, passport,
                    )
                )
                handoffs.clear()
                controller.readmit_prior_provider_failures([claim])
        self.assertEqual(
            handoffs,
            [((cell,), {
                "baseline": input_head,
                "head": handoff_head,
                "policy": CONTROL._handoff_policy(ticket),
                "role": "reviewer",
            })],
        )

        self.assertEqual(
            (claim["status"], claim["receipt"], claim["role"]),
            ("running", receipt_digest, "reviewer"),
        )
        self.assertNotIn(ticket, controller.prior_transition_tickets)
        pending = copy.deepcopy(claim)
        calls = []
        controller.direct_model_identity_candidate = lambda *_args: False
        controller.emit_attempt_terminal = lambda *_args: calls.append("terminal")
        controller.json_call = lambda *args, **_kwargs: (
            calls.append(args[:2])
            or {"failed_run_id": terminal["run_id"]}
        )
        controller.migrate_passport = lambda *_args: calls.append("passport")
        controller.event = lambda name, *_args, **_kwargs: calls.append(name)
        self.assertTrue(controller.finish_pending_run(claim))
        self.assertEqual(
            (claim["status"], claim["receipt"], claim["role"]),
            ("claimed", "", ""),
        )
        self.assertIn(("models", "fallback-auto"), calls)
        self.assertIn("provider_fallback", calls)

        claim = pending
        controller.prior_transition_tickets.add(ticket)
        controller.terminal_for_receipt = lambda *_args: {
            **terminal, "route_id": "codex-gpt-5.6-sol",
        }
        with patch.dict(os.environ, environment):
            controller.readmit_prior_provider_failures([claim])
        self.assertIn(ticket, controller.prior_transition_tickets)

        controller.terminal_for_receipt = lambda *_args: terminal
        for name, qualification, mode in (
            ("production", None, ""),
            (
                "initial",
                {"generation": 1, "mode": "initial", "tickets": [ticket]},
                "isolated",
            ),
            (
                "takeover",
                {"generation": 1, "mode": "successor", "tickets": [ticket]},
                "takeover",
            ),
        ):
            with self.subTest(authority=name):
                claim = copy.deepcopy(pending)
                controller.qualification = qualification
                controller.prior_transition_tickets.add(ticket)
                with patch.dict(os.environ, {
                    "FACTORY_KIT_TRUST_SCOPE": (
                        "qualification-candidate" if qualification else ""
                    ),
                    "FACTORY_QUALIFICATION_MODE": mode,
                }):
                    controller.readmit_prior_provider_failures([claim])
                self.assertIn(ticket, controller.prior_transition_tickets)
                self.assertEqual(
                    (claim["status"], claim["receipt"], claim["role"]),
                    ("running", receipt_digest, "reviewer"),
                )

    def test_changed_state_machine_refusal_is_readmitted_ticket_locally(self) -> None:
        controller = CONTROL.Controller(self.args)
        ticket = "T-110"
        branch = f"ticket/{ticket}"
        cell = self.root / "cell-refusal"
        subprocess.run(["git", "init", "-q", "-b", branch, str(cell)], check=True)
        ticket_path = cell / f"factory/tickets/{ticket}.md"
        route_path = cell / f"factory/route-plans/{ticket}.json"
        ticket_path.parent.mkdir(parents=True)
        route_path.parent.mkdir(parents=True)
        ticket_path.write_text("State: Review\nKit-SHA: " + self.release.name + "\n")
        route_path.write_text(json.dumps({
            "kit_sha": self.release.name, "ticket": ticket,
        }) + "\n")
        subprocess.run(["git", "-C", str(cell), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(cell), "-c", "user.name=Factory",
            "-c", "user.email=factory@example.invalid", "commit", "-qm", "ticket",
        ], check=True)
        head = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD"], text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD^{tree}"], text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        ticket_blob = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", f"HEAD:factory/tickets/{ticket}.md"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        protected = head
        subprocess.run([
            "git", "-C", str(cell), "update-ref", "refs/remotes/origin/main", head,
        ], check=True)
        old_base = "e" * 40
        route_digest = hashlib.sha256(route_path.read_bytes()).hexdigest()
        key = self.state / "passport.key"
        key.write_bytes(b"k" * 32)
        key.chmod(0o600)
        passport = PASSPORT.authenticate({
            "branch": branch, "contract_version": "1.8.0",
            "current_state": "Review", "factory_sha": self.release.name,
            "head_sha": head, "project": "relay", "publication_state": "validating",
            "protected_base_sha": old_base,
            "route_plan_sha256": route_digest,
            "schema": "nysa.software-factory.ticket-passport/v1", "ticket": ticket,
        }, key.read_bytes())
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        passport_path = passports / f"{ticket}.json"
        PASSPORT.write_atomic(passport_path, passport)
        passport_file = hashlib.sha256(passport_path.read_bytes()).hexdigest()

        def refusal(
            base: str | None, stage: str = "REFUSE refused protected input",
        ) -> dict:
            value = {
                "branch": branch, "consumed": False, "contract_version": "1.8.0",
                "evidence_sha256": "1" * 64, "factory_sha": self.release.name,
                "head_sha": head, "head_tree": tree, "lease_sha256": "2" * 64,
                "loop": None, "nonce": "3" * 32, "passport_sha256": passport_file,
                "product_origin_sha256": "4" * 64, "project": "relay",
                "role": None,
                "route_plan_sha256": route_digest,
                "schema": "nysa.software-factory.transition-receipt/v1",
                "stage": stage, "ticket": ticket, "ticket_blob": ticket_blob,
            }
            if base is not None:
                value["protected_base_sha"] = base
            value["receipt_sha256"] = hashlib.sha256(STATE.canonical({
                key: item for key, item in value.items()
                if key not in {"consumed", "consumed_at_epoch", "receipt_sha256"}
            })).hexdigest()
            CONTROL.write(self.state / f"{ticket}.json", value)
            return value

        claim = {
            "blocked_reason": "state-machine-refusal", "branch": branch,
            "lease": "", "parked": True, "priority": "normal",
            "publication_lease": "", "receipt": "", "role": "",
            "schema": CONTROL.CLAIM_SCHEMA, "status": "blocked", "ticket": ticket,
            "worktree": str(cell),
        }
        sibling = {**claim, "status": "claimed", "ticket": "T-111"}
        controller.ticket_release_current = lambda _claim: True
        controller.remote_passport_valid = lambda _claim: True
        calls = []
        controller.json_call = lambda *args, **_kwargs: calls.append(args)

        for name, prepare in (
            ("unchanged", lambda: refusal(protected)),
            ("malformed", lambda: refusal("")),
            ("dirty", lambda: (refusal(old_base), (cell / "dirty").write_text("x"))),
            ("foreign", lambda: (refusal(old_base), passport.update(branch="ticket/T-999"))),
        ):
            with self.subTest(name=name):
                claim.update(status="blocked", blocked_reason="state-machine-refusal")
                prepare()
                if name == "foreign":
                    PASSPORT.write_atomic(passport_path, PASSPORT.authenticate({
                        key: item for key, item in passport.items()
                        if key not in {"authentication_sha256", "passport_sha256"}
                    }, key.read_bytes()))
                controller.recover_changed_state_machine_refusals([claim], protected)
                self.assertEqual(claim["status"], "blocked")
                self.assertEqual(calls, [])
                (cell / "dirty").unlink(missing_ok=True)
                passport["branch"] = branch
                PASSPORT.write_atomic(passport_path, passport)

        refused = refusal(None)
        self.assertNotIn("protected_base_sha", refused)
        controller.remote_passport_valid = lambda _claim: False
        controller.recover_changed_state_machine_refusals([claim], protected)
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(calls, [])
        controller.remote_passport_valid = lambda _claim: True
        lease = "5" * 64

        def accepted_transition(refused, accepted_lease, nonce):
            current = refusal(
                protected,
                "REFUSE dependency refresh required; "
                f"dependencies=T-109; protected-main={protected}",
            )
            current.update(
                lease_sha256=hashlib.sha256(accepted_lease.encode()).hexdigest(),
                parent_digest=refused["receipt_sha256"], nonce=nonce,
            )
            current["receipt_sha256"] = hashlib.sha256(STATE.canonical({
                key: item for key, item in current.items()
                if key not in {"consumed", "consumed_at_epoch", "receipt_sha256"}
            })).hexdigest()
            CONTROL.write(self.state / f"{ticket}.json", current)
            return state_transition(
                current["stage"], current["receipt_sha256"], ticket,
            )

        def failed_cleanup(*arguments, **_kwargs):
            if arguments[0] == "claim":
                return {"lease_id": lease, "schema_version": 1, "ticket": ticket}
            if arguments[0] == "state-machine":
                return state_transition(refused["stage"], refused["receipt_sha256"], ticket)
            if arguments[0] == "release":
                raise CONTROL.ControllerError("lease cleanup unavailable")
            self.fail(arguments)

        controller.json_call = failed_cleanup
        controller.recover_changed_state_machine_refusals([claim], protected)
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(claim["lease"], lease)
        self.assertEqual(
            CONTROL.read(controller.claim_path(ticket))["lease"], lease,
        )
        claim["lease"] = ""

        def json_call(*arguments, **_kwargs):
            calls.append(arguments)
            if arguments[0] == "claim":
                return {"lease_id": lease, "schema_version": 1, "ticket": ticket}
            if arguments[0] == "state-machine":
                return accepted_transition(refused, lease, "6" * 32)
            self.fail(arguments)

        controller.json_call = json_call
        controller.recover_changed_state_machine_refusals(
            [claim, sibling], protected,
        )

        self.assertEqual(claim["status"], "claimed")
        self.assertNotIn("blocked_reason", claim)
        self.assertEqual(claim["lease"], lease)
        self.assertEqual(sibling["status"], "claimed")
        self.assertEqual([call[0] for call in calls], ["claim", "state-machine"])

        attempt_path = self.state / f"refusal-readmission-{ticket}.json"
        real_write = CONTROL.write
        for boundary, crash_after_receipt in (
            ("lease-acquired", False), ("child-receipt", True),
        ):
            with self.subTest(interruption=boundary):
                attempt_path.unlink(missing_ok=True)
                refused = refusal(None)
                interrupted_lease = (
                    "7" * 64 if not crash_after_receipt else "8" * 64
                )
                records = {}
                claim.update(
                    blocked_reason="state-machine-refusal", lease="",
                    receipt="", role="", status="blocked",
                )
                claim.pop("lease_released", None)
                CONTROL.write(controller.claim_path(ticket), claim)

                def interrupted_call(*arguments, **_kwargs):
                    if arguments[0] == "claim":
                        records[ticket] = {"lease_id": interrupted_lease}
                        return {
                            "lease_id": interrupted_lease,
                            "schema_version": 1, "ticket": ticket,
                        }
                    if arguments[0] == "state-machine":
                        return accepted_transition(
                            refused, interrupted_lease, "9" * 32,
                        )
                    self.fail(arguments)

                interrupted = CONTROL.Controller(self.args)
                interrupted.ticket_release_current = lambda _claim: True
                interrupted.remote_passport_valid = lambda _claim: True
                interrupted.dispatcher_lease_records = lambda: records
                interrupted.json_call = interrupted_call
                if crash_after_receipt:
                    interrupted.save_claim = lambda _claim: (_ for _ in ()).throw(
                        KeyboardInterrupt("crash after child receipt")
                    )
                    crash = self.assertRaisesRegex(
                        KeyboardInterrupt, "crash after child receipt",
                    )
                else:
                    def interrupted_write(path, value):
                        if path == attempt_path and value.get("lease"):
                            raise KeyboardInterrupt("crash after lease acquisition")
                        real_write(path, value)

                    crash = self.assertRaisesRegex(
                        KeyboardInterrupt, "crash after lease acquisition",
                    )
                with crash:
                    if crash_after_receipt:
                        interrupted.recover_changed_state_machine_refusals(
                            [claim], protected,
                        )
                    else:
                        with patch.object(
                            CONTROL, "write", side_effect=interrupted_write,
                        ):
                            interrupted.recover_changed_state_machine_refusals(
                                [claim], protected,
                            )
                persisted = CONTROL.read(controller.claim_path(ticket))
                self.assertEqual(persisted["status"], "blocked")
                claim.clear()
                claim.update(persisted)
                self.assertEqual(CONTROL.read(attempt_path)["lease"], (
                    interrupted_lease if crash_after_receipt else ""
                ))

                restarted = CONTROL.Controller(self.args)
                restarted.ticket_release_current = lambda _claim: True
                restarted.remote_passport_valid = lambda _claim: True
                restarted.dispatcher_lease_records = lambda: records

                def adopt(*arguments, **_kwargs):
                    if not crash_after_receipt and arguments[0] == "state-machine":
                        return accepted_transition(
                            refused, interrupted_lease, "a" * 32,
                        )
                    self.fail(f"restart unexpectedly called launcher: {arguments}")

                restarted.json_call = adopt
                restarted.recover_changed_state_machine_refusals(
                    [claim], protected,
                )
                self.assertEqual(claim["status"], "claimed")
                self.assertEqual(claim["lease"], interrupted_lease)
                self.assertFalse(attempt_path.exists())

        for index, invalidation in enumerate((
            "dirty", "remote-diverged", "protected-main-advanced",
        ), 11):
            with self.subTest(interrupted_invalidation=invalidation):
                attempt_path.unlink(missing_ok=True)
                refused = refusal(None)
                interrupted_lease = f"{index:064x}"
                records = {}
                claim.update(
                    blocked_reason="state-machine-refusal", lease="",
                    receipt="", role="", status="blocked",
                )
                claim.pop("lease_released", None)
                CONTROL.write(controller.claim_path(ticket), claim)

                def acquire(*arguments, **_kwargs):
                    if arguments[0] == "claim":
                        records[ticket] = {"lease_id": interrupted_lease}
                        return {
                            "lease_id": interrupted_lease,
                            "schema_version": 1, "ticket": ticket,
                        }
                    self.fail(arguments)

                interrupted = CONTROL.Controller(self.args)
                interrupted.ticket_release_current = lambda _claim: True
                interrupted.remote_passport_valid = lambda _claim: True
                interrupted.dispatcher_lease_records = lambda: records
                interrupted.json_call = acquire

                def interrupt_marker_write(path, value):
                    if path == attempt_path and value.get("lease"):
                        raise KeyboardInterrupt("crash after lease acquisition")
                    real_write(path, value)

                with self.assertRaisesRegex(
                    KeyboardInterrupt, "crash after lease acquisition",
                ):
                    with patch.object(
                        CONTROL, "write", side_effect=interrupt_marker_write,
                    ):
                        interrupted.recover_changed_state_machine_refusals(
                            [claim], protected,
                        )
                if invalidation == "dirty":
                    (cell / "dirty").write_text("dirty", encoding="utf-8")

                released = []
                restarted = CONTROL.Controller(self.args)
                restarted.ticket_release_current = lambda _claim: True
                restarted.remote_passport_valid = lambda _claim: (
                    invalidation != "remote-diverged"
                )
                restarted.dispatcher_lease_records = lambda: records

                def release(*arguments, **_kwargs):
                    if arguments[0] == "release":
                        released.append(arguments)
                        if invalidation == "remote-diverged":
                            raise CONTROL.ControllerError(
                                "lease cleanup unavailable"
                            )
                        records.pop(ticket)
                        return {"released": True, "ticket": ticket}
                    self.fail(arguments)

                restarted.json_call = release
                restarted.recover_changed_state_machine_refusals(
                    [claim],
                    "f" * 40
                    if invalidation == "protected-main-advanced" else protected,
                )
                self.assertEqual(claim["status"], "blocked")
                self.assertEqual(len(released), 1)
                if invalidation == "remote-diverged":
                    self.assertEqual(claim["lease"], interrupted_lease)
                    self.assertEqual(
                        CONTROL.read(controller.claim_path(ticket))["lease"],
                        interrupted_lease,
                    )
                    self.assertIn(ticket, records)
                else:
                    self.assertNotIn(ticket, records)
                self.assertFalse(attempt_path.exists())
                (cell / "dirty").unlink(missing_ok=True)

        for index, invalidation in enumerate(("canceled", "terminal"), 21):
            with self.subTest(prefilter_invalidation=invalidation):
                attempt_path.unlink(missing_ok=True)
                refusal(None)
                interrupted_lease = f"{index:064x}"
                records = {}
                claim.update(
                    blocked_reason="state-machine-refusal", lease="",
                    receipt="", role="", status="blocked",
                )
                claim.pop("lease_released", None)
                CONTROL.write(controller.claim_path(ticket), claim)

                def acquire(*arguments, **_kwargs):
                    if arguments[0] == "claim":
                        records[ticket] = {"lease_id": interrupted_lease}
                        return {
                            "lease_id": interrupted_lease,
                            "schema_version": 1, "ticket": ticket,
                        }
                    self.fail(arguments)

                interrupted = CONTROL.Controller(self.args)
                interrupted.ticket_release_current = lambda _claim: True
                interrupted.remote_passport_valid = lambda _claim: True
                interrupted.dispatcher_lease_records = lambda: records
                interrupted.json_call = acquire

                def interrupt_marker_write(path, value):
                    if path == attempt_path and value.get("lease"):
                        raise KeyboardInterrupt("crash after lease acquisition")
                    real_write(path, value)

                with self.assertRaisesRegex(
                    KeyboardInterrupt, "crash after lease acquisition",
                ):
                    with patch.object(
                        CONTROL, "write", side_effect=interrupt_marker_write,
                    ):
                        interrupted.recover_changed_state_machine_refusals(
                            [claim], protected,
                        )
                if invalidation == "terminal":
                    claim["status"] = "claimed"
                    CONTROL.write(controller.claim_path(ticket), claim)

                restarted = CONTROL.Controller(self.args)
                restarted.dispatcher_lease_records = lambda: records
                restarted.product_ticket_canceled = lambda *_args: (
                    invalidation == "canceled"
                )
                restarted.product_ticket_done = lambda _ticket: (
                    invalidation == "terminal"
                )

                def release(*arguments, **_kwargs):
                    if arguments[0] == "release":
                        if invalidation == "canceled":
                            raise CONTROL.ControllerError(
                                "lease cleanup unavailable"
                            )
                        records.pop(ticket)
                        return {"released": True, "ticket": ticket}
                    self.fail(arguments)

                restarted.json_call = release
                pending = restarted.reconcile_refusal_readmission_markers(
                    [claim], protected,
                )
                if invalidation == "canceled":
                    self.assertEqual(pending, {ticket})
                    self.assertEqual(claim["status"], "blocked")
                    self.assertEqual(
                        claim["blocked_reason"],
                        "state-machine-refusal-cleanup",
                    )
                    self.assertEqual(claim["lease"], interrupted_lease)
                    self.assertIn(ticket, records)
                else:
                    self.assertEqual(pending, set())
                    self.assertEqual(claim["status"], "claimed")
                    self.assertNotIn(ticket, records)
                self.assertFalse(attempt_path.exists())

    def test_reconciliation_marker_accepts_exact_passport_successor(self) -> None:
        controller = CONTROL.Controller(self.args)
        ticket = "T-110"
        cell = self.root / "cell-reconciliation-successor"
        subprocess.run(["git", "init", "-q", str(cell)], check=True)
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        old = {
            "branch": f"ticket/{ticket}",
            "factory_sha": self.release.name,
            "head_sha": "b" * 40,
            "passport_sha256": "c" * 64,
            "protected_base_sha": "d" * 40,
            "route_plan_sha256": "e" * 64,
            "ticket": ticket,
        }
        passport_path = passports / f"{ticket}.json"
        CONTROL.write(passport_path, old)
        claim = {
            "branch": f"ticket/{ticket}", "lease": "a" * 64,
            "priority": "normal", "publication_lease": "", "receipt": "",
            "role": "", "schema": CONTROL.CLAIM_SCHEMA, "status": "claimed",
            "ticket": ticket, "worktree": str(cell),
        }
        controller.mark_reconciling(claim)
        marker = CONTROL.read(controller.reconciliation_marker(ticket))
        current = {
            **old,
            "factory_release_history": [{
                "contract_version": "1.8.0",
                "factory_sha": self.release.name,
            }],
            "head_sha": "f" * 40,
            "passport_sha256": "1" * 64,
            "parent_digest": old["passport_sha256"],
            "parent_file_sha256": "2" * 64,
            "protected_base_sha": "3" * 40,
            "route_plan_sha256": "4" * 64,
            "migration_history": [{
                "from_factory_sha": self.release.name,
                "from_head_sha": old["head_sha"],
                "from_passport_file_sha256": "2" * 64,
                "from_passport_sha256": old["passport_sha256"],
                "from_protected_base_sha": old["protected_base_sha"],
                "from_route_plan_sha256": old["route_plan_sha256"],
                "schema": CONTROL.PASSPORT_MIGRATION_SCHEMA,
                "to_factory_sha": self.release.name,
                "to_head_sha": "f" * 40,
                "to_protected_base_sha": "3" * 40,
                "to_route_plan_sha256": "4" * 64,
            }],
        }
        CONTROL.write(passport_path, current)
        controller.remote_passport_valid = lambda _claim: True

        with patch.object(
            CONTROL, "write", side_effect=OSError("crash before marker write"),
        ):
            with self.assertRaisesRegex(OSError, "crash before marker write"):
                controller.mark_reconciling(claim)
        self.assertEqual(
            CONTROL.read(controller.reconciliation_marker(ticket)), marker
        )
        advanced = CONTROL.read(controller.reconciliation_marker(ticket))
        events = [
            CONTROL.read(path) for path in controller.events.glob("*.json")
            if CONTROL.read(path).get("event")
            == "reconciliation_boundary_refresh_authorized"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["from_head_sha"], old["head_sha"])
        self.assertEqual(events[0]["head_sha"], current["head_sha"])
        controller.mark_reconciling(claim)
        advanced = CONTROL.read(controller.reconciliation_marker(ticket))
        self.assertEqual(advanced["head_sha"], current["head_sha"])
        self.assertEqual(
            advanced["passport_sha256"], current["passport_sha256"]
        )
        controller.mark_reconciling(claim)
        self.assertEqual(len([
            path for path in controller.events.glob("*.json")
            if CONTROL.read(path).get("event")
            == "reconciliation_boundary_refresh_authorized"
        ]), 1)
        edge = current["migration_history"][0]
        invalid = (
            ({**marker, "head_sha": "5" * 40}, advanced, current),
            (
                marker,
                {**advanced, "run_snapshot_sha256": "6" * 64},
                current,
            ),
            (marker, advanced, {**current, "parent_digest": "7" * 64}),
            (
                marker,
                advanced,
                {**current, "route_plan_sha256": "8" * 64},
            ),
            (
                marker,
                advanced,
                {
                    **current,
                    "migration_history": [{
                        **edge, "to_factory_sha": "9" * 40,
                    }],
                },
            ),
        )
        for old_boundary, new_boundary, candidate in invalid:
            self.assertFalse(controller.reconciliation_boundary_successor(
                claim, old_boundary, new_boundary, candidate,
            ))
        (cell / "dirty").write_text("dirty", encoding="utf-8")
        self.assertFalse(controller.reconciliation_boundary_successor(
            claim, marker, advanced, current,
        ))
        (cell / "dirty").unlink()

        CONTROL.write(controller.reconciliation_marker(ticket), marker)
        controller.remote_passport_valid = lambda _claim: False
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "reconciliation boundary conflicts"
        ):
            controller.mark_reconciling(claim)
        self.assertEqual(
            CONTROL.read(controller.reconciliation_marker(ticket)), marker
        )

    def test_reconciliation_boundary_conflict_does_not_block_sibling(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        controller.protected_main_head = lambda: "f" * 40
        claims = []
        for number, ticket in enumerate(("T-110", "T-111"), 1):
            cell = self.root / f"cell-reconciliation-{number}"
            route = cell / f"factory/route-plans/{ticket}.json"
            route.parent.mkdir(parents=True)
            route.write_text("{}\n", encoding="utf-8")
            claims.append({
                "branch": f"ticket/{ticket}",
                "lease": f"{number:064x}",
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "claimed",
                "ticket": ticket,
                "worktree": str(cell),
            })
        controller.load_claims = lambda: claims
        controller.recover_missing_passport_claims = lambda _claims: None
        controller.recover_upgraded_claims = lambda _claims: None
        controller.recover_repaired_failures = lambda _claims: None
        controller.claim_new = lambda current: current
        controller.pin_routes = lambda _claims: []
        controller.event = lambda *_args, **_kwargs: None
        cleanup_attempts = []

        def cleanup_failure(name):
            cleanup_attempts.append(name)
            raise CONTROL.ControllerError(f"{name} cleanup unavailable")

        def mark(claim):
            if claim["ticket"] == "T-110":
                raise CONTROL.ControllerError(
                    "ticket reconciliation boundary conflicts"
                )

        reconciled = []
        controller.withdraw_publication = lambda _claim: cleanup_failure(
            "publication"
        )
        controller.release_ticket_lease = lambda _claim: cleanup_failure(
            "lease"
        )
        controller.mark_reconciling = mark
        controller.reconcile_ticket = lambda claim: (
            reconciled.append(claim["ticket"])
            or {"status": "waiting", "ticket": claim["ticket"]}
        )

        result = controller.reconcile()

        by_ticket = {item["ticket"]: item for item in result["results"]}
        self.assertEqual(result["status"], "ok")
        self.assertEqual(by_ticket["T-110"]["status"], "blocked")
        self.assertEqual(
            claims[0]["blocked_reason"], "reconciliation-boundary"
        )
        self.assertEqual(cleanup_attempts, ["publication", "lease"])
        self.assertNotIn("lease_released", claims[0])
        self.assertEqual(reconciled, ["T-111"])
        self.assertEqual(by_ticket["T-111"]["status"], "waiting")

    def test_reconciliation_marker_accepts_exact_release_suffix(self) -> None:
        source = CONTROL.Controller(self.args)
        target_release = self.root / ("9" * 40)
        target_release.mkdir()
        target_args = copy.copy(self.args)
        target_args.release_path = target_release
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)

        for ticket, route_migrated in (("T-120", False), ("T-121", True)):
            cell = self.root / f"cell-release-suffix-{ticket}"
            subprocess.run(["git", "init", "-q", str(cell)], check=True)
            old = {
                "branch": f"ticket/{ticket}",
                "factory_sha": self.release.name,
                "head_sha": "b" * 40,
                "passport_sha256": "c" * 64,
                "protected_base_sha": "d" * 40,
                "route_plan_sha256": "e" * 64,
                "ticket": ticket,
            }
            passport_path = passports / f"{ticket}.json"
            CONTROL.write(passport_path, old)
            claim = {
                "branch": f"ticket/{ticket}", "lease": "a" * 64,
                "priority": "normal", "publication_lease": "",
                "receipt": "", "role": "", "schema": CONTROL.CLAIM_SCHEMA,
                "status": "claimed", "ticket": ticket,
                "worktree": str(cell),
            }
            source.mark_reconciling(claim)
            marker = CONTROL.read(source.reconciliation_marker(ticket))
            refresh = {
                "from_factory_sha": self.release.name,
                "from_head_sha": old["head_sha"],
                "from_passport_file_sha256": "2" * 64,
                "from_passport_sha256": old["passport_sha256"],
                "from_protected_base_sha": old["protected_base_sha"],
                "from_route_plan_sha256": old["route_plan_sha256"],
                "schema": CONTROL.PASSPORT_MIGRATION_SCHEMA,
                "to_factory_sha": self.release.name,
                "to_head_sha": "f" * 40,
                "to_protected_base_sha": "3" * 40,
                "to_route_plan_sha256": "4" * 64,
            }
            release = {
                "from_factory_sha": self.release.name,
                "from_head_sha": refresh["to_head_sha"],
                "from_passport_file_sha256": "6" * 64,
                "from_passport_sha256": "5" * 64,
                "from_protected_base_sha": refresh["to_protected_base_sha"],
                "from_route_plan_sha256": refresh["to_route_plan_sha256"],
                "schema": CONTROL.PASSPORT_MIGRATION_SCHEMA,
                "to_factory_sha": target_release.name,
                "to_head_sha": refresh["to_head_sha"],
                "to_protected_base_sha": refresh["to_protected_base_sha"],
                "to_route_plan_sha256": refresh["to_route_plan_sha256"],
            }
            migrations = [refresh, release]
            current = {
                **old,
                "factory_release_history": [
                    {
                        "contract_version": "1.8.0",
                        "factory_sha": self.release.name,
                    },
                    {
                        "contract_version": "1.8.0",
                        "factory_sha": target_release.name,
                    },
                ],
                "factory_sha": target_release.name,
                "head_sha": release["to_head_sha"],
                "migration_history": migrations,
                "parent_digest": release["from_passport_sha256"],
                "parent_file_sha256": release["from_passport_file_sha256"],
                "passport_sha256": "1" * 64,
                "protected_base_sha": release["to_protected_base_sha"],
                "route_plan_sha256": release["to_route_plan_sha256"],
            }
            if route_migrated:
                route = {
                    "from_factory_sha": target_release.name,
                    "from_head_sha": current["head_sha"],
                    "from_passport_file_sha256": "a" * 64,
                    "from_passport_sha256": "8" * 64,
                    "from_protected_base_sha": current["protected_base_sha"],
                    "from_route_plan_sha256": current["route_plan_sha256"],
                    "schema": CONTROL.PASSPORT_MIGRATION_SCHEMA,
                    "to_factory_sha": target_release.name,
                    "to_head_sha": "7" * 40,
                    "to_protected_base_sha": current["protected_base_sha"],
                    "to_route_plan_sha256": "9" * 64,
                }
                migrations = [*migrations, route]
                current.update({
                    "head_sha": route["to_head_sha"],
                    "migration_history": migrations,
                    "parent_digest": route["from_passport_sha256"],
                    "parent_file_sha256": route["from_passport_file_sha256"],
                    "passport_sha256": "0" * 64,
                    "route_plan_sha256": route["to_route_plan_sha256"],
                })
            CONTROL.write(passport_path, current)
            target = CONTROL.Controller(target_args)
            target.remote_passport_valid = lambda _claim: True

            target.mark_reconciling(claim)

            advanced = CONTROL.read(target.reconciliation_marker(ticket))
            self.assertEqual(advanced["factory_sha"], target_release.name)
            self.assertEqual(advanced["head_sha"], current["head_sha"])
            invalid = (
                {**current, "migration_history": [refresh, *migrations]},
                {
                    **current,
                    "migration_history": [
                        refresh,
                        {**release, "from_head_sha": "0" * 40},
                        *migrations[2:],
                    ],
                },
                {
                    **current,
                    "factory_release_history": current[
                        "factory_release_history"
                    ][:-1],
                },
                {
                    **current,
                    "migration_history": [{
                        **refresh, "from_passport_sha256": "f" * 64,
                    }, *migrations[1:]],
                },
            )
            for candidate in invalid:
                self.assertFalse(target.reconciliation_boundary_successor(
                    claim, marker, advanced, candidate,
                ))

    def test_prior_maintenance_receipt_admits_only_exact_migrated_successor(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        ticket = "T-110"
        source_factory = "b" * 40
        cell = self.root / f"parked/{ticket}"
        remote = self.root / "prior-maintenance.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        subprocess.run(
            ["git", "init", "-q", "-b", f"ticket/{ticket}", str(cell)],
            check=True,
        )
        route = cell / f"factory/route-plans/{ticket}.json"
        ticket_path = cell / f"factory/tickets/{ticket}.md"
        route.parent.mkdir(parents=True)
        ticket_path.parent.mkdir(parents=True)
        route.write_text(CONTROL.canonical({
            "kit_sha": source_factory,
            "schema": "ticket-model-route-plan/v1",
            "ticket": ticket,
        }) + "\n", encoding="utf-8")
        ticket_path.write_text(
            f"# {ticket}\n\nState: Review\nKit-SHA: {source_factory}\n",
            encoding="utf-8",
        )
        pin = cell / "factory/KIT_PIN"
        pin.write_text(source_factory + "\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(cell), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(cell), "-c", "user.name=Factory",
            "-c", "user.email=factory@example.invalid", "commit", "-qm", "old",
        ], check=True)
        subprocess.run(
            ["git", "-C", str(cell), "remote", "add", "origin", str(remote)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(cell), "push", "-q", "-u", "origin", "HEAD"],
            check=True,
        )
        old_head = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        old_tree = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD^{tree}"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        old_ticket = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", f"HEAD:{ticket_path.relative_to(cell)}"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        old_route = hashlib.sha256(route.read_bytes()).hexdigest()
        source_file = "c" * 64
        source_digest = "d" * 64
        receipt = {
            "branch": f"ticket/{ticket}",
            "consumed": False,
            "contract_version": "1.8.0",
            "factory_sha": source_factory,
            "head_sha": old_head,
            "head_tree": old_tree,
            "lease_sha256": "e" * 64,
            "loop": None,
            "nonce": "f" * 32,
            "parent_digest": "0" * 64,
            "passport_sha256": source_file,
            "product_origin_sha256": "1" * 64,
            "project": "relay",
            "role": None,
            "route_plan_sha256": old_route,
            "schema": "nysa.software-factory.transition-receipt/v1",
            "stage": (
                "REFUSE MAINTENANCE file present — "
                "factory control plane is paused"
            ),
            "ticket": ticket,
            "ticket_blob": old_ticket,
        }
        receipt["receipt_sha256"] = hashlib.sha256(CONTROL.canonical_document({
            key: value for key, value in receipt.items() if key != "consumed"
        })).hexdigest()
        CONTROL.write(self.state / f"{ticket}.json", receipt)

        route.write_text(CONTROL.canonical({
            "kit_sha": self.release.name,
            "schema": "ticket-model-route-plan/v1",
            "ticket": ticket,
        }) + "\n", encoding="utf-8")
        ticket_path.write_text(
            f"# {ticket}\n\nState: Review\nKit-SHA: {self.release.name}\n",
            encoding="utf-8",
        )
        pin.write_text(self.release.name + "\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(cell), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(cell), "-c", "user.name=Factory",
            "-c", "user.email=factory@example.invalid", "commit", "-qm", "route",
        ], check=True)
        subprocess.run(
            ["git", "-C", str(cell), "push", "-q", "origin", "HEAD"],
            check=True,
        )
        head = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD^{tree}"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        protected = "2" * 40
        release_edge = {
            "from_factory_sha": source_factory,
            "from_head_sha": old_head,
            "from_passport_file_sha256": source_file,
            "from_passport_sha256": source_digest,
            "from_protected_base_sha": "3" * 40,
            "from_route_plan_sha256": old_route,
            "schema": CONTROL.PASSPORT_MIGRATION_SCHEMA,
            "to_factory_sha": self.release.name,
            "to_head_sha": old_head,
            "to_protected_base_sha": protected,
            "to_route_plan_sha256": old_route,
        }
        route_edge = {
            "from_factory_sha": self.release.name,
            "from_head_sha": old_head,
            "from_passport_file_sha256": "4" * 64,
            "from_passport_sha256": "5" * 64,
            "from_protected_base_sha": protected,
            "from_route_plan_sha256": old_route,
            "schema": CONTROL.PASSPORT_MIGRATION_SCHEMA,
            "to_factory_sha": self.release.name,
            "to_head_sha": head,
            "to_protected_base_sha": protected,
            "to_route_plan_sha256": hashlib.sha256(route.read_bytes()).hexdigest(),
        }
        key = self.state / "passport.key"
        key.write_bytes(b"k" * 32)
        key.chmod(0o600)
        passport = PASSPORT.authenticate({
            "base_history": [old_head, head],
            "branch": f"ticket/{ticket}",
            "charge_records": [],
            "completed_role_evidence": [],
            "contract_version": "1.8.0",
            "current_stage": "RUN reviewer",
            "current_state": "Review",
            "factory_release_history": [
                {"contract_version": "1.8.0", "factory_sha": source_factory},
                {"contract_version": "1.8.0", "factory_sha": self.release.name},
            ],
            "factory_sha": self.release.name,
            "head_sha": head,
            "head_tree": tree,
            "migration_history": [release_edge, route_edge],
            "parent_digest": route_edge["from_passport_sha256"],
            "parent_file_sha256": route_edge["from_passport_file_sha256"],
            "product_origin_sha256": "1" * 64,
            "project": "relay",
            "protected_base_sha": protected,
            "publication_state": "validating",
            "route_plan_sha256": route_edge["to_route_plan_sha256"],
            "schema": "nysa.software-factory.ticket-passport/v1",
            "ticket": ticket,
            "ticket_blob": subprocess.run(
                ["git", "-C", str(cell), "rev-parse", f"HEAD:{ticket_path.relative_to(cell)}"],
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
            "transition_receipt_sha256": receipt["receipt_sha256"],
        }, key.read_bytes())
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        PASSPORT.write_atomic(passports / f"{ticket}.json", passport)
        claim = {
            "branch": f"ticket/{ticket}", "lease": "6" * 64,
            "parked": True, "priority": "normal", "publication_lease": "",
            "receipt": "", "role": "", "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed", "ticket": ticket, "worktree": str(cell),
        }
        complete = {
            "factory_sha": self.release.name,
            "schema": CONTROL.EVENT_SCHEMA,
            "ticket": ticket,
        }
        CONTROL.write(
            self.state / (
                f"passport-route-migration-complete-{ticket}-"
                f"{self.release.name}.json"
            ),
            complete,
        )
        CONTROL.write(controller.reconciliation_marker(ticket), {
            "branch": claim["branch"],
            "factory_sha": source_factory,
            "head_sha": old_head,
            "passport_sha256": source_digest,
            "run_snapshot_sha256": controller.ticket_run_snapshot(ticket),
            "schema": "nysa.software-factory.reconciliation-boundary/v1",
            "ticket": ticket,
        })
        controller.locally_valid_operator_passport = lambda _claim: passport

        self.assertEqual(
            controller.prior_maintenance_receipt_successor(claim), receipt,
        )

        (self.product / "factory/MAINTENANCE").touch()
        self.assertIsNone(controller.prior_maintenance_receipt_successor(claim))
        (self.product / "factory/MAINTENANCE").unlink()
        claim["role"] = "reviewer"
        self.assertIsNone(controller.prior_maintenance_receipt_successor(claim))
        claim["role"] = ""

        for migration_history in (
            [release_edge, release_edge, route_edge],
            [{**release_edge, "to_head_sha": "7" * 40}, route_edge],
        ):
            invalid = {
                key: copy.deepcopy(value) for key, value in passport.items()
                if key not in {"authentication_sha256", "passport_sha256"}
            }
            invalid["migration_history"] = migration_history
            invalid = PASSPORT.authenticate(invalid, key.read_bytes())
            PASSPORT.write_atomic(passports / f"{ticket}.json", invalid)
            controller.locally_valid_operator_passport = lambda _claim, value=invalid: value
            self.assertIsNone(
                controller.prior_maintenance_receipt_successor(claim)
            )
        PASSPORT.write_atomic(passports / f"{ticket}.json", passport)
        controller.locally_valid_operator_passport = lambda _claim: passport
        (cell / "dirty").write_text("dirty\n", encoding="utf-8")
        self.assertIsNone(controller.prior_maintenance_receipt_successor(claim))
        (cell / "dirty").unlink()

        passport_file = hashlib.sha256(
            (passports / f"{ticket}.json").read_bytes()
        ).hexdigest()

        def issue_current(
            stage: str = "RUN reviewer", *, response_loss: bool = False,
        ) -> dict:
            current = {
                "branch": claim["branch"],
                "consumed": False,
                "contract_version": "1.8.0",
                "evidence_sha256": "8" * 64,
                "factory_sha": self.release.name,
                "head_sha": head,
                "head_tree": tree,
                "lease_sha256": hashlib.sha256(
                    claim["lease"].encode()
                ).hexdigest(),
                "loop": None,
                "nonce": "9" * 32,
                "parent_digest": receipt["receipt_sha256"],
                "passport_sha256": passport_file,
                "product_origin_sha256": "1" * 64,
                "project": "relay",
                "role": STATE.stage_role(stage),
                "route_plan_sha256": passport["route_plan_sha256"],
                "schema": "nysa.software-factory.transition-receipt/v1",
                "stage": stage,
                "ticket": ticket,
                "ticket_blob": passport["ticket_blob"],
            }
            current["receipt_sha256"] = hashlib.sha256(
                CONTROL.canonical_document({
                    key: value for key, value in current.items()
                    if key != "consumed"
                })
            ).hexdigest()
            CONTROL.write(self.state / f"{ticket}.json", current)
            if response_loss:
                raise CONTROL.ControllerError("state-machine response lost")
            return state_transition(stage, current["receipt_sha256"], ticket)

        controller.ensure_lease = lambda *_args: None
        controller.prior_transition_tickets.add(ticket)
        with patch.object(
            controller, "event_once", side_effect=OSError("event unavailable"),
        ):
            controller.recover_prior_maintenance_receipts([claim])
        self.assertIn(ticket, controller.prior_transition_tickets)
        self.assertEqual(
            CONTROL.read(self.state / f"{ticket}.json"), receipt,
        )

        controller.json_call = lambda *_args, **_kwargs: issue_current()
        controller.recover_prior_maintenance_receipts([claim])
        self.assertNotIn(ticket, controller.prior_transition_tickets)
        current = CONTROL.read(self.state / f"{ticket}.json")
        self.assertEqual(current["parent_digest"], receipt["receipt_sha256"])
        self.assertEqual(current["factory_sha"], self.release.name)
        events = [
            CONTROL.read(path) for path in controller.events.glob("*.json")
            if CONTROL.read(path).get("event")
            == "prior_maintenance_receipt_recovery_authorized"
        ]
        self.assertEqual(len(events), 1)

        CONTROL.write(self.state / f"{ticket}.json", receipt)
        controller.prior_transition_tickets.add(ticket)
        controller.json_call = lambda *_args, **_kwargs: issue_current(
            response_loss=True,
        )
        controller.recover_prior_maintenance_receipts([claim])
        self.assertNotIn(ticket, controller.prior_transition_tickets)
        self.assertEqual(
            CONTROL.read(self.state / f"{ticket}.json")["parent_digest"],
            receipt["receipt_sha256"],
        )

        CONTROL.write(self.state / f"{ticket}.json", receipt)
        controller.prior_transition_tickets.add(ticket)
        maintenance_stage = (
            "REFUSE MAINTENANCE file present — factory control plane is paused"
        )
        controller.json_call = lambda *_args, **_kwargs: issue_current(
            maintenance_stage,
        )
        controller.recover_prior_maintenance_receipts([claim])
        self.assertNotIn(ticket, controller.prior_transition_tickets)
        controller.reconciliation_marker(ticket).unlink()
        restarted = CONTROL.Controller(self.args)
        self.assertEqual(
            restarted.operator_transition(claim)["factory_sha"],
            self.release.name,
        )
        self.assertEqual(restarted.prior_transition_tickets, set())
        restarted.mark_reconciling(claim)
        self.assertEqual(
            CONTROL.read(restarted.reconciliation_marker(ticket))[
                "factory_sha"
            ],
            self.release.name,
        )

    def test_worker_error_recovers_from_exact_reconciliation_boundary(self) -> None:
        controller = CONTROL.Controller(self.args)
        ticket = "T-110"
        cell = self.root / "parked/T-110"
        cell.parent.mkdir()
        subprocess.run(["git", "init", "-q", str(cell)], check=True)
        passport = {
            "branch": f"ticket/{ticket}",
            "factory_sha": self.release.name,
            "head_sha": "b" * 40,
            "passport_sha256": "c" * 64,
            "ticket": ticket,
        }
        (self.state / "passports").mkdir(mode=0o700)
        CONTROL.write(self.state / f"passports/{ticket}.json", passport)
        claim = {
            "branch": f"ticket/{ticket}", "lease": "d" * 64,
            "parked": True, "priority": "normal", "publication_lease": "",
            "receipt": "", "role": "", "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed", "ticket": ticket, "worktree": str(cell),
        }
        controller.mark_reconciling(claim)
        claim.update(status="blocked", blocked_reason="worker-error")
        controller.save_claim(claim)
        controller.ticket_release_current = lambda _claim: True
        controller.remote_passport_valid = lambda _claim: True
        leases = []
        controller.ensure_lease = lambda item, label: leases.append(label)

        controller.recover_interrupted_claims([claim])
        controller.recover_interrupted_claims([claim])

        self.assertEqual(claim["status"], "claimed")
        self.assertNotIn("blocked_reason", claim)
        self.assertEqual(leases, ["interrupted-reconciliation"])
        self.assertFalse(controller.reconciliation_marker(ticket).exists())
        events = [
            CONTROL.read(path) for path in controller.events.glob("*.json")
            if CONTROL.read(path).get("event") == "worker_error_recovered"
        ]
        self.assertEqual(len(events), 1)

    def test_reconciliation_cleanup_preserves_causal_error(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = {
            "branch": "ticket/T-110", "lease": "", "priority": "normal",
            "publication_lease": "", "receipt": "", "role": "",
            "schema": CONTROL.CLAIM_SCHEMA, "status": "claimed",
            "ticket": "T-110", "worktree": str(self.root / "cell-1"),
        }
        controller.ensure_lease = lambda *_args: (_ for _ in ()).throw(
            CONTROL.ControllerError("launch lock stuck")
        )
        controller.withdraw_publication = lambda *_args: None
        controller.release_ticket_lease = lambda *_args: (_ for _ in ()).throw(
            CONTROL.ControllerError("factory-launch: invalid dispatcher lease")
        )

        result = controller.reconcile_ticket(claim)

        self.assertEqual(result, {
            "error": "launch lock stuck", "status": "error", "ticket": "T-110",
        })
        event = next(
            CONTROL.read(path) for path in controller.events.glob("*.json")
            if CONTROL.read(path).get("event") == "controller_error"
        )
        self.assertEqual(event["error"], "launch lock stuck")
        self.assertEqual(event["cleanup_deferred"], [])

    def test_interrupted_two_ticket_recovery_is_independent(self) -> None:
        controller = CONTROL.Controller(self.args)
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        claims = []
        for ticket in ("T-093", "T-100"):
            cell = self.root / f"cell-{ticket}"
            subprocess.run(["git", "init", "-q", str(cell)], check=True)
            CONTROL.write(passports / f"{ticket}.json", {
                "branch": f"ticket/{ticket}",
                "factory_sha": self.release.name,
                "head_sha": "b" * 40,
                "passport_sha256": hashlib.sha256(ticket.encode()).hexdigest(),
                "ticket": ticket,
            })
            claim = {
                "branch": f"ticket/{ticket}",
                "lease": hashlib.sha256(f"lease-{ticket}".encode()).hexdigest(),
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "claimed",
                "ticket": ticket,
                "worktree": str(cell),
            }
            controller.mark_reconciling(claim)
            claim["status"] = "blocked"
            controller.save_claim(claim)
            claims.append(claim)
        controller.ticket_release_current = lambda _claim: True
        controller.remote_passport_valid = lambda _claim: True
        controller.json_call = lambda *_args, **_kwargs: {}

        controller.recover_interrupted_claims(claims)
        self.assertEqual([claim["status"] for claim in claims], ["claimed", "claimed"])
        self.assertFalse(any(
            controller.reconciliation_marker(claim["ticket"]).exists()
            for claim in claims
        ))

    def test_progressed_ticket_advances_while_sibling_is_still_active(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.protected_main_head = lambda: "f" * 40
        claims = []
        for number, ticket in enumerate(("T-110", "T-111"), 1):
            cell = self.root / f"cell-{number}"
            cell.mkdir()
            claims.append({
                "branch": f"ticket/{ticket}",
                "lease": f"{number:064x}",
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "claimed",
                "ticket": ticket,
                "worktree": str(cell),
            })
        controller.load_claims = lambda: claims
        controller.recover_missing_passport_claims = lambda _claims: None
        controller.recover_upgraded_claims = lambda _claims: None
        controller.recover_repaired_failures = lambda _claims: None
        controller.claim_new = lambda current: current
        controller.pin_routes = lambda _claims: []
        controller.event = lambda *_args, **_kwargs: None
        advanced = __import__("threading").Event()
        calls = {"T-110": 0, "T-111": 0}

        def reconcile(claim):
            ticket = claim["ticket"]
            calls[ticket] += 1
            if ticket == "T-110" and calls[ticket] == 1:
                return {"status": "progressed", "ticket": ticket}
            if ticket == "T-110":
                advanced.set()
                return {"status": "waiting", "ticket": ticket}
            self.assertTrue(advanced.wait(1), "sibling checkpoint did not advance")
            return {"status": "waiting", "ticket": ticket}

        controller.reconcile_ticket = reconcile
        result = controller.reconcile()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls, {"T-110": 2, "T-111": 1})

    def test_qualification_complete_subset_waits_for_protected_target(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {
            "target_done": 3,
            "tickets": ["T-110", "T-111", "T-112"],
        }
        controller.capacity = 3
        cell = self.root / "cell-1"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        claim = {
            "branch": "ticket/T-110", "lease": "1" * 64,
            "priority": "normal", "publication_lease": "", "receipt": "",
            "role": "", "schema": CONTROL.CLAIM_SCHEMA, "status": "claimed",
            "ticket": "T-110", "worktree": str(cell),
        }
        claims = [claim]
        done = set()
        controller.load_claims = lambda: list(claims)
        controller.qualification_admission_preflight = lambda _claims: None
        controller.qualification_marker = lambda *_args, **_kwargs: True
        controller.clear_admission_failure = lambda: None
        controller.record_qualification_done_targets = lambda: None
        controller.recover_missing_passport_claims = lambda _claims: None
        controller.recover_upgraded_claims = lambda _claims: None
        controller.recover_terminal_exports = lambda _claims: None
        controller.recover_repaired_failures = lambda _claims: None
        controller.claim_new = lambda current, *_args: current
        controller.pin_routes = lambda _claims: []
        controller.event = lambda *_args, **_kwargs: None
        controller.mark_reconciling = lambda _claim: None
        refreshed = []
        controller.protected_main_head = lambda: refreshed.append(True) or "f" * 40

        def protected_done(ticket):
            if done:
                self.assertEqual(refreshed, [True])
            return ticket in done

        controller.product_ticket_done = protected_done

        def complete(item):
            done.add(item["ticket"])
            claims.clear()
            return {"status": "complete", "ticket": item["ticket"]}

        controller.reconcile_ticket_until_wait = complete
        result = controller.reconcile()

        self.assertEqual(result["status"], "waiting_for_target", result)
        self.assertEqual(
            result["results"], [{"status": "complete", "ticket": "T-110"}],
        )
        self.assertEqual(refreshed, [True])

    def test_qualification_empty_restart_recovers_protected_targets(self) -> None:
        controller = CONTROL.Controller(self.args)
        tickets = ["T-110", "T-111", "T-112"]
        controller.qualification = {"target_done": 3, "tickets": tickets}
        controller.capacity = 3
        controller.load_claims = lambda: []
        controller.qualification_admission_preflight = lambda _claims: None
        controller.qualification_marker = lambda *_args, **_kwargs: True
        controller.clear_admission_failure = lambda: None
        controller.record_qualification_done_targets = lambda: None
        controller.recover_missing_passport_claims = lambda _claims: None
        controller.recover_upgraded_claims = lambda _claims: None
        controller.recover_terminal_exports = lambda _claims: None
        controller.recover_repaired_failures = lambda _claims: None
        controller.claim_new = lambda current, *_args: current
        controller.pin_routes = lambda _claims: []
        controller.event = lambda *_args, **_kwargs: None
        done = set(tickets)
        refreshed = []
        observed = []
        controller.protected_main_head = lambda: refreshed.append(True) or "f" * 40

        def protected_done(ticket):
            observed.append(len(refreshed))
            return ticket in done

        controller.product_ticket_done = protected_done

        recovered = controller.reconcile()
        done.clear()
        unfinished = controller.reconcile()

        self.assertEqual(recovered["status"], "ok")
        self.assertEqual(recovered["results"], [
            {"status": "complete", "ticket": ticket} for ticket in tickets
        ])
        self.assertEqual(unfinished["status"], "ok")
        self.assertEqual(unfinished["results"], [])
        self.assertEqual(refreshed, [True, True])
        self.assertEqual(observed, [1, 1, 1, 2, 2, 2])

    def test_qualification_restart_surfaces_durable_blocked_claims(self) -> None:
        controller = CONTROL.Controller(self.args)
        tickets = ["T-110", "T-111", "T-112", "T-113"]
        controller.qualification = {"target_done": 4, "tickets": tickets}
        claims = [
            {
                "status": status, "ticket": ticket,
                "worktree": str(self.root / "parked" / ticket),
            }
            for ticket, status in zip(
                tickets, ("blocked", "budget", "blocked", "blocked"),
                strict=True,
            )
        ]
        claims[2]["blocked_reason"] = "route-migration-required"
        claims[3].update(
            blocked_reason="recovery-abandoned:release-upgrade",
            lease_released=True,
            recovery_attempt={
                "count": CONTROL.RECOVERY_ATTEMPT_LIMIT,
                "factory_sha": controller.release_path.name,
                "input_sha256": "a" * 64,
                "outcome_sha256": "b" * 64,
                "phase": "abandoned",
                "recovery": "release-upgrade",
                "retry_reason": "route-migration-required",
                "retry_status": "blocked",
            },
        )
        controller.load_claims = lambda: claims
        controller.qualification_admission_preflight = lambda _claims: None
        controller.qualification_marker = lambda *_args, **_kwargs: True
        controller.cancellation_authority = lambda _claims: None
        controller.retire_canceled_claims = lambda current, *_args: current
        controller.quarantine_invalid_transition_claims = lambda _claims: None
        controller.reclaim_orphaned_execution_cells = lambda _claims: None
        excluded = False

        def operator_transition(claim):
            if excluded and claim["ticket"] == "T-112":
                controller.invalid_transition_tickets.add(claim["ticket"])
            if excluded and claim["ticket"] == "T-113":
                controller.prior_transition_tickets.add(claim["ticket"])

        controller.operator_transition = operator_transition
        controller.release_inactive_ticket_leases = lambda _claims: None
        controller.recover_changed_state_machine_refusals = lambda *_args: None
        controller.recover_operator_action_events = lambda _claims: None
        controller.record_qualification_done_targets = lambda: None
        controller.recover_missing_passport_claims = lambda _claims: None
        controller.recover_terminal_requests = lambda _claims: None
        controller.readmit_prior_provider_failures = lambda _claims: None
        controller.recover_each = lambda *_args, **_kwargs: None
        controller.recover_prior_maintenance_receipts = lambda _claims: None
        controller.claim_new = lambda current, *_args: current
        controller.clear_admission_failure = lambda: None
        controller.maintain_successor_leases = lambda _claims: None
        controller.pin_routes = lambda _claims: []
        controller.product_ticket_done = lambda _ticket: False
        controller.event = lambda *_args, **_kwargs: None
        controller.role_active = lambda _claim: False

        migration = controller.reconcile()
        claims[2].pop("blocked_reason")
        claims[3].pop("blocked_reason")
        claims[3].pop("lease_released")
        claims[3].pop("recovery_attempt")
        excluded = True
        transition = controller.reconcile()
        claims[0]["blocked_reason"] = "external-unavailable"
        external = controller.reconcile()

        expected = [
            {"status": "blocked", "ticket": "T-110"},
            {"status": "budget", "ticket": "T-111"},
        ]
        self.assertEqual(migration["results"], expected)
        self.assertEqual(transition["results"], expected)
        self.assertEqual(external["results"], [
            {
                "status": "waiting", "ticket": "T-110",
                "wait_reason": "external-unavailable",
            },
            {"status": "budget", "ticket": "T-111"},
        ])
        self.assertIn("T-112", controller.invalid_transition_tickets)
        self.assertIn("T-113", controller.prior_transition_tickets)

    def test_qualification_controller_error_stops_sibling_next_role_launches(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {
            "target_done": 3,
            "tickets": ["T-110", "T-111", "T-112"],
        }
        controller.capacity = 2
        controller.protected_main_head = lambda: "f" * 40
        claims = []
        for number, ticket in enumerate(controller.qualification["tickets"], 1):
            cell = self.root / f"cell-{number}"
            route = cell / f"factory/route-plans/{ticket}.json"
            route.parent.mkdir(parents=True)
            route.write_text("{}\n", encoding="utf-8")
            claims.append({
                "branch": f"ticket/{ticket}",
                "lease": f"{number:064x}",
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "claimed",
                "ticket": ticket,
                "worktree": str(cell),
            })
        controller.load_claims = lambda: claims
        controller.qualification_admission_preflight = lambda _claims: None
        controller.qualification_marker = lambda *_args, **_kwargs: True
        controller.record_qualification_done_targets = lambda: None
        controller.recover_missing_passport_claims = lambda _claims: None
        controller.recover_upgraded_claims = lambda _claims: None
        controller.recover_terminal_exports = lambda _claims: None
        controller.recover_repaired_failures = lambda _claims: None
        controller.claim_new = lambda current, *_args: current
        controller.pin_routes = lambda _claims: []
        controller.event = lambda *_args, **_kwargs: None
        controller.park_claim = lambda _claim: True
        controller.save_claim = lambda _claim: None
        controller.withdraw_publication = lambda _claim: None
        controller.release_ticket_lease = lambda _claim: None
        barrier = threading.Barrier(2)
        calls = {claim["ticket"]: 0 for claim in claims}
        real_reconcile = controller.reconcile_ticket

        def ensure_lease(claim, _label):
            if claim["ticket"] == "T-110":
                barrier.wait(timeout=2)
                raise CONTROL.ControllerError("causal controller error")

        controller.ensure_lease = ensure_lease

        def reconcile(claim):
            ticket = claim["ticket"]
            calls[ticket] += 1
            if ticket == "T-112":
                raise AssertionError("queued sibling started after cohort error")
            if calls[ticket] > 1:
                raise AssertionError("sibling started its next role after cohort error")
            if ticket == "T-110":
                return real_reconcile(claim)
            barrier.wait(timeout=2)
            self.assertTrue(
                controller.qualification_cohort_error.wait(1),
                "cohort error was not latched",
            )
            return {"status": "progressed", "ticket": ticket}

        controller.reconcile_ticket = reconcile
        result = controller.reconcile()

        self.assertEqual(result["status"], "error")
        self.assertEqual(calls, {"T-110": 1, "T-111": 1, "T-112": 0})
        self.assertEqual(
            next(item for item in result["results"] if item["ticket"] == "T-110"),
            {
                "error": "causal controller error",
                "status": "error",
                "ticket": "T-110",
            },
        )

    def test_qualification_worker_exception_latches_before_sibling_next_role(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {"tickets": ["T-110", "T-111"]}
        controller.park_claim = lambda _claim: True
        barrier = threading.Barrier(2)
        calls = {"T-110": 0, "T-111": 0}
        claims = [
            {"receipt": "", "ticket": ticket}
            for ticket in calls
        ]

        def reconcile(claim):
            ticket = claim["ticket"]
            calls[ticket] += 1
            if calls[ticket] > 1:
                raise AssertionError("sibling started after worker exception")
            barrier.wait(timeout=2)
            if ticket == "T-110":
                raise RuntimeError("worker defect")
            self.assertTrue(
                controller.qualification_cohort_error.wait(1),
                "worker exception did not latch in its worker",
            )
            return {"status": "progressed", "ticket": ticket}

        controller.reconcile_ticket = reconcile
        with ThreadPoolExecutor(max_workers=2) as executor:
            failed = executor.submit(
                controller.reconcile_ticket_until_wait, claims[0]
            )
            sibling = executor.submit(
                controller.reconcile_ticket_until_wait, claims[1]
            )
            with self.assertRaisesRegex(RuntimeError, "worker defect"):
                failed.result(timeout=2)
            self.assertEqual(
                sibling.result(timeout=2).get("wait_reason"),
                "qualification-cohort-error",
            )
        self.assertEqual(calls, {"T-110": 1, "T-111": 1})

    def test_qualification_latch_blocks_role_at_atomic_launch_gate(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {"tickets": ["T-110"]}
        claim = {
            "lease": "1" * 64,
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        controller.ensure_execution_cell = lambda _claim: None
        controller.save_claim = lambda _claim: None
        events = []
        controller.event = lambda name, *_args, **_kwargs: events.append(name)
        preflight_started = threading.Event()
        release_preflight = threading.Event()

        def preflight(*_args, **_kwargs):
            preflight_started.set()
            self.assertTrue(release_preflight.wait(1))
            return {"exit_code": 0, "status": "ok"}

        controller.json_call = preflight
        with (
            patch.object(CONTROL, "ensure_qualification_artifacts"),
            patch.object(CONTROL.subprocess, "Popen") as popen,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            role = executor.submit(
                controller.run_role, claim, "planner", "b" * 64, []
            )
            self.assertTrue(preflight_started.wait(1))
            controller.latch_qualification_cohort_error()
            release_preflight.set()
            self.assertFalse(role.result(timeout=2))

        popen.assert_not_called()
        self.assertNotIn("attempt_started", events)
        self.assertNotIn("receipt", claim)

    def test_qualification_protected_mutation_latches_before_sibling_launch(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {"tickets": ["T-110", "T-111"]}
        failed = {
            "branch": "ticket/T-110", "lease": "a" * 64,
            "publication_lease": "", "receipt": "b" * 64,
            "role": "spec-linter", "schema": CONTROL.CLAIM_SCHEMA,
            "status": "running", "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        sibling = {
            "lease": "c" * 64, "ticket": "T-111",
            "worktree": str(self.root / "cell-2"),
        }
        terminal = {
            "accounting_state": "completed", "exit_status": "11",
            "go_issued": "1",
            "role_exit": "role_exit_protected_ticket_mutation",
            "route_id": "cursor-claude-opus-5-thinking-medium",
            "run_id": "protected-mutation", "task_submitted": "1",
            "terminal_reason_code": "",
        }
        controller.terminal_for_receipt = (
            lambda ticket, _receipt: terminal if ticket == "T-110" else None
        )
        controller.emit_attempt_terminal = lambda *_args: None
        controller.terminal_already_exported = lambda *_args: False
        controller.passport = lambda *_args: self.assertTrue(
            controller.qualification_cohort_error.is_set()
        )
        controller.archive_emergency_admission = lambda *_args: None
        controller.save_claim = lambda *_args: None
        controller.release_ticket_lease = lambda *_args: None
        controller.passport_sha256 = lambda *_args: "d" * 64
        preflight_started = threading.Event()
        release_preflight = threading.Event()

        def json_call(*_args, **_kwargs):
            preflight_started.set()
            self.assertTrue(release_preflight.wait(1))
            return {"exit_code": 0, "status": "ok"}

        controller.json_call = json_call
        controller.ensure_execution_cell = lambda _claim: None

        def event(name, *_args, **_kwargs):
            if name == "role_blocked":
                self.assertTrue(controller.qualification_cohort_error.is_set())

        controller.event = event
        with (
            patch.object(CONTROL, "ensure_qualification_artifacts"),
            patch.object(CONTROL.subprocess, "Popen") as popen,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            role = executor.submit(
                controller.run_role, sibling, "planner", "e" * 64, []
            )
            self.assertTrue(preflight_started.wait(1))
            self.assertFalse(controller.finish_pending_run(failed))
            release_preflight.set()
            self.assertFalse(role.result(timeout=2))

        popen.assert_not_called()
        self.assertEqual(failed["blocked_reason"], "role-failure")
        self.assertTrue(controller.qualification_cohort_error.is_set())

    def test_qualification_latch_accounts_existing_terminal_before_stopping(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {"tickets": ["T-110"]}
        claim = {"receipt": "b" * 64, "ticket": "T-110"}
        calls = []
        controller.ensure_lease = lambda _claim, label: calls.append(label)

        def finish(_claim):
            calls.append("finish")
            claim["status"] = "blocked"
            return False

        controller.finish_pending_run = finish
        parked = []
        controller.park_claim = lambda item: parked.append(item["ticket"]) or True
        controller.role_active = lambda _claim: False
        controller.settle_recovery_attempt = lambda _claim: False
        controller.run_role = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(AssertionError("successor role launched"))
        )
        controller.qualification_cohort_error.set()

        result = controller.reconcile_ticket_until_wait(claim)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(calls, ["terminal-accounting", "finish"])
        self.assertEqual(parked, ["T-110"])

    def test_qualification_spend_limit_latches_and_preserves_dirty_failure(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {"tickets": ["T-110"]}
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "publication_lease": "",
            "receipt": "b" * 64,
            "role": "test-author",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "running",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        terminal = {
            "accounting_state": "completed",
            "exit_status": "1",
            "go_issued": "1",
            "role_exit": "provider_failed",
            "route_id": "claude-fable",
            "run_id": "spend-limit",
            "task_submitted": "1",
            "terminal_reason_code": "provider_spend_limit",
        }
        calls = []
        controller.ensure_lease = lambda *_args: None
        controller.role_active = lambda _claim: False
        controller.terminal_for_receipt = lambda *_args: terminal
        controller.emit_attempt_terminal = lambda *_args: calls.append("terminal")
        controller.cell_git = lambda *_args: subprocess.CompletedProcess(
            [], 0, " M apps/web/tests/example.test.tsx\0", ""
        )
        controller.passport = lambda *_args: (
            (_ for _ in ()).throw(AssertionError("dirty failure checkpointed"))
        )
        controller.archive_emergency_admission = lambda *_args: None
        controller.save_claim = lambda *_args: calls.append("save")
        controller.release_ticket_lease = lambda *_args: calls.append("release")
        controller.passport_sha256 = lambda *_args: "c" * 64
        controller.event = (
            lambda name, _ticket, **details: calls.append((name, details))
        )
        controller.park_claim = lambda *_args: calls.append("park") or False
        controller.settle_recovery_attempt = lambda *_args: False

        result = controller.reconcile_ticket_until_wait(claim)

        self.assertEqual(result, {"status": "blocked", "ticket": "T-110"})
        self.assertTrue(controller.qualification_cohort_error.is_set())
        self.assertEqual(claim["blocked_reason"], "role-failure")
        self.assertIn("release", calls)
        self.assertIn("park", calls)
        blocked = next(item for item in calls if isinstance(item, tuple))
        self.assertEqual(blocked[0], "role_blocked")
        self.assertEqual(
            blocked[1]["terminal_reason_code"], "provider_spend_limit"
        )

    def test_scheduler_tracks_each_concurrent_ticket_once(self) -> None:
        import threading

        controller = CONTROL.Controller(self.args)
        controller.protected_main_head = lambda: "f" * 40
        claims = []
        for number, ticket in enumerate(("T-110", "T-111", "T-112"), 1):
            cell = self.root / f"cell-{number}"
            route = cell / f"factory/route-plans/{ticket}.json"
            route.parent.mkdir(parents=True)
            route.write_text("{}\n", encoding="utf-8")
            claims.append({
                "branch": f"ticket/{ticket}",
                "lease": f"{number:064x}",
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "claimed",
                "ticket": ticket,
                "worktree": str(cell),
            })
        controller.load_claims = lambda: claims
        controller.recover_missing_passport_claims = lambda _claims: None
        controller.recover_upgraded_claims = lambda _claims: None
        controller.recover_terminal_exports = lambda _claims: None
        controller.recover_repaired_failures = lambda _claims: None
        controller.claim_new = lambda current: current
        controller.pin_routes = lambda _claims: []
        controller.event = lambda *_args, **_kwargs: None
        barrier = threading.Barrier(3)
        lock = threading.Lock()
        live = 0
        peak = 0
        calls = {claim["ticket"]: 0 for claim in claims}

        def reconcile(claim):
            nonlocal live, peak
            with lock:
                calls[claim["ticket"]] += 1
                live += 1
                peak = max(peak, live)
            barrier.wait(timeout=2)
            with lock:
                live -= 1
            return {"status": "waiting", "ticket": claim["ticket"]}

        controller.reconcile_ticket = reconcile
        result = controller.reconcile()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls, {"T-110": 1, "T-111": 1, "T-112": 1})
        self.assertEqual(peak, 3)

    def test_transient_gate_waiters_recheck_while_sibling_worker_is_live(
        self,
    ) -> None:
        import threading
        import time

        controller = CONTROL.Controller(self.args)
        controller.protected_main_head = lambda: "f" * 40
        claims = []
        for number, ticket in enumerate(("T-110", "T-111", "T-112"), 1):
            cell = self.root / f"cell-{number}"
            route = cell / f"factory/route-plans/{ticket}.json"
            route.parent.mkdir(parents=True)
            route.write_text("{}\n", encoding="utf-8")
            claims.append({
                "branch": f"ticket/{ticket}",
                "lease": f"{number:064x}",
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "claimed",
                "ticket": ticket,
                "worktree": str(cell),
            })
        controller.load_claims = lambda: claims
        controller.recover_missing_passport_claims = lambda _claims: None
        controller.recover_upgraded_claims = lambda _claims: None
        controller.recover_terminal_exports = lambda _claims: None
        controller.recover_repaired_failures = lambda _claims: None
        controller.claim_new = lambda current: current
        controller.pin_routes = lambda _claims: []
        controller.event = lambda *_args, **_kwargs: None
        waited = {ticket: threading.Event() for ticket in ("T-110", "T-111")}
        calls = {ticket: 0 for ticket in ("T-110", "T-111", "T-112")}

        def reconcile(claim):
            ticket = claim["ticket"]
            calls[ticket] += 1
            if ticket in waited:
                waited[ticket].set()
                if calls[ticket] == 1:
                    return {
                        "status": "waiting", "ticket": ticket,
                        "wait_reason": (
                            "pr-gate" if ticket == "T-110"
                            else "publication-lease"
                        ),
                    }
            else:
                self.assertTrue(all(event.wait(1) for event in waited.values()))
                time.sleep(0.1)
            return {"status": "waiting", "ticket": ticket}

        controller.reconcile_ticket = reconcile
        with patch.object(CONTROL, "RECONCILE_INTERVAL_SECONDS", 0.02):
            result = controller.reconcile()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls, {"T-110": 2, "T-111": 2, "T-112": 1})

    def test_scheduler_wakes_new_ticket_while_provider_future_is_live(self) -> None:
        import threading

        controller = CONTROL.Controller(self.args)
        controller.protected_main_head = lambda: "f" * 40
        claims = []
        for number, ticket in enumerate(("T-110", "T-111"), 1):
            cell = self.root / f"cell-{number}"
            route = cell / f"factory/route-plans/{ticket}.json"
            route.parent.mkdir(parents=True)
            route.write_text("{}\n", encoding="utf-8")
            claims.append({
                "branch": f"ticket/{ticket}",
                "lease": f"{number:064x}",
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "claimed",
                "ticket": ticket,
                "worktree": str(cell),
            })
        first_started = threading.Event()
        expose_second = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()
        calls = {"T-110": 0, "T-111": 0}

        def load_claims():
            return claims if expose_second.is_set() else claims[:1]

        controller.load_claims = load_claims
        controller.recover_missing_passport_claims = lambda _claims: None
        controller.recover_upgraded_claims = lambda _claims: None
        controller.recover_terminal_exports = lambda _claims: None
        controller.recover_repaired_failures = lambda _claims: None
        controller.claim_new = lambda current: current
        controller.pin_routes = lambda _claims: []
        controller.event = lambda *_args, **_kwargs: None

        def reconcile(claim):
            ticket = claim["ticket"]
            calls[ticket] += 1
            if ticket == "T-110":
                first_started.set()
                # Expose the second claim while this future is still live, so the
                # scheduler wake is observed without racing a timed helper thread.
                expose_second.set()
                self.assertTrue(release_first.wait(2))
            else:
                self.assertTrue(first_started.is_set())
                second_started.set()
                release_first.set()
            return {"status": "waiting", "ticket": ticket}

        controller.reconcile_ticket = reconcile

        with patch.object(CONTROL, "RECONCILE_INTERVAL_SECONDS", 0.02):
            result = controller.reconcile()
        self.assertTrue(second_started.is_set())
        self.assertEqual(calls, {"T-110": 1, "T-111": 1})
        self.assertEqual(result["status"], "ok")

    def test_restart_does_not_resubmit_externally_active_role(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.protected_main_head = lambda: "f" * 40
        claims = []
        for number, ticket in enumerate(("T-110", "T-111"), 1):
            cell = self.root / f"cell-{number}"
            route = cell / f"factory/route-plans/{ticket}.json"
            route.parent.mkdir(parents=True)
            route.write_text("{}\n", encoding="utf-8")
            claims.append({
                "branch": f"ticket/{ticket}",
                "lease": f"{number:064x}",
                "priority": "normal",
                "publication_lease": "",
                "receipt": "a" * 64 if ticket == "T-110" else "",
                "role": "builder" if ticket == "T-110" else "",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "running" if ticket == "T-110" else "claimed",
                "ticket": ticket,
                "worktree": str(cell),
            })
        controller.load_claims = lambda: claims
        controller.recover_missing_passport_claims = lambda _claims: None
        controller.recover_upgraded_claims = lambda _claims: None
        controller.recover_terminal_exports = lambda _claims: None
        controller.recover_repaired_failures = lambda _claims: None
        controller.claim_new = lambda current: current
        controller.pin_routes = lambda _claims: []
        controller.event = lambda *_args, **_kwargs: None
        controller.role_active = lambda claim: claim["ticket"] == "T-110"
        called = []
        controller.reconcile_ticket = lambda claim: (
            called.append(claim["ticket"])
            or {"status": "waiting", "ticket": claim["ticket"]}
        )
        controller.reconcile()
        self.assertEqual(called, ["T-111"])

    def test_restart_does_not_reattach_parked_ticket_when_cells_are_full(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.protected_main_head = lambda: "f" * 40
        controller.capacity = 3
        claims = []
        for number, ticket in enumerate(("T-110", "T-111", "T-112"), 1):
            claims.append({
                "branch": f"ticket/{ticket}",
                "lease": f"{number:064x}",
                "priority": "normal",
                "publication_lease": "",
                "receipt": "a" * 64,
                "role": "builder",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "running",
                "ticket": ticket,
                "worktree": str(self.root / f"cell-{number}"),
            })
        parked = {
            "branch": "ticket/T-113",
            "lease": "",
            "parked": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "waiting",
            "ticket": "T-113",
            "worktree": str(self.root / "parked/T-113"),
        }
        claims.append(parked)
        route = Path(parked["worktree"]) / "factory/route-plans/T-113.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        controller.load_claims = lambda: claims
        controller.recover_missing_passport_claims = lambda _claims: None
        controller.recover_upgraded_claims = lambda _claims: None
        controller.recover_terminal_exports = lambda _claims: None
        controller.recover_repaired_failures = lambda _claims: None
        controller.claim_new = lambda current: current
        controller.pin_routes = lambda _claims: []
        controller.event = lambda *_args, **_kwargs: None
        controller.role_active = lambda claim: claim["ticket"] != "T-113"
        called = []
        controller.reconcile_ticket = lambda claim: called.append(claim["ticket"])

        result = controller.reconcile()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(called, [])
        self.assertEqual(parked["status"], "waiting")
        self.assertTrue(parked["parked"])

    def test_clean_checkpoint_parks_frees_capacity_and_reattaches(self) -> None:
        import subprocess

        run = lambda *command, cwd=None: subprocess.run(
            command, cwd=cwd, text=True, capture_output=True, check=True
        )
        run("git", "init", "-q", "-b", "main", cwd=self.product)
        run("git", "config", "user.name", "Test", cwd=self.product)
        run("git", "config", "user.email", "test@example.invalid", cwd=self.product)
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "seed", cwd=self.product)
        run("git", "branch", "ticket/T-110", cwd=self.product)
        cells = self.root / "cells"
        cells.mkdir(mode=0o700)
        cell = cells / "cell-1"
        run(
            "git", "worktree", "add", "-q", str(cell), "ticket/T-110",
            cwd=self.product,
        )
        controller = CONTROL.Controller(self.args)
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        controller.remote_passport_valid = lambda _claim: True
        calls = []

        def json_call(*arguments, **_kwargs):
            calls.append(arguments)
            if arguments[0] == "release":
                return {}
            if arguments[0] == "claim":
                return {
                    "lease_id": "b" * 64,
                    "schema_version": 1,
                    "ticket": "T-110",
                }
            raise AssertionError(arguments)

        controller.json_call = json_call
        controller.event = lambda *_args, **_kwargs: None
        self.assertTrue(controller.park_claim(claim))
        self.assertEqual(claim["status"], "waiting")
        self.assertEqual(claim["lease"], "")
        self.assertTrue(claim["parked"])
        self.assertFalse(CONTROL.Controller.consumes_capacity(claim))
        self.assertFalse(cell.exists())
        self.assertTrue((cells / "parked/T-110").is_dir())

        admissions = iter(({
            "action": "START",
            "branch": "ticket/T-111",
            "lease_id": "c" * 64,
            "priority": "normal",
            "ticket": "T-111",
            "worktree": str(cell),
        }, {"action": "WAIT"}))

        def admission(*args, **_kwargs):
            if args[:2] == ("dispatch-plan", "--shadow"):
                return {"action": "SHADOW", "ticket": "T-111"}
            if args[:2] == ("models", "plan"):
                return self.healthy_model_plan()
            return next(admissions)

        controller.json_call = admission
        admitted = controller.claim_new([claim])
        self.assertIn("T-111", {item["ticket"] for item in admitted})

        controller.json_call = json_call
        controller.ensure_lease(claim, "paid-role")
        controller.ensure_execution_cell(claim)
        self.assertNotIn("parked", claim)
        self.assertEqual(claim["lease"], "b" * 64)
        self.assertEqual(claim["worktree"], str(cell))
        self.assertTrue(cell.is_dir())

    def test_reclaims_only_clean_claimless_inactive_execution_cells(self) -> None:
        run = lambda *command, cwd=None: subprocess.run(
            command, cwd=cwd, text=True, capture_output=True, check=True
        )
        run("git", "init", "-q", "-b", "main", cwd=self.product)
        run("git", "config", "user.name", "Test", cwd=self.product)
        run("git", "config", "user.email", "test@example.invalid", cwd=self.product)
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "seed", cwd=self.product)
        cells = self.root / "cells"
        cells.mkdir(mode=0o700)
        for number, ticket in enumerate(("T-110", "T-111", "T-112", "T-113"), 1):
            run("git", "branch", f"ticket/{ticket}", cwd=self.product)
            run(
                "git", "worktree", "add", "-q", str(cells / f"cell-{number}"),
                f"ticket/{ticket}", cwd=self.product,
            )
        run(
            "git", "worktree", "add", "-q", "--detach", str(cells / "cell-5"),
            "HEAD", cwd=self.product,
        )
        portable = self.root / "missing-portable-worktree"
        run(
            "git", "worktree", "add", "-q", "--detach", str(portable),
            "HEAD", cwd=self.product,
        )
        run(
            "git", "worktree", "lock", "--reason", "portable",
            str(portable), cwd=self.product,
        )
        shutil.rmtree(portable)
        (cells / "cell-2/dirty").write_text("preserve\n", encoding="utf-8")
        active = self.product / "factory/.active-runs/T-113.builder.lock"
        active.parent.mkdir(mode=0o700)
        active.mkdir(mode=0o700)
        (active / "owner").write_text(
            "pid=1\nprocess_start=test\ntoken=" + "a" * 32 + "\n",
            encoding="utf-8",
        )
        claim = {
            "branch": "ticket/T-112", "ticket": "T-112",
            "worktree": str(cells / "cell-3"),
        }
        self.args.worktree_root = cells
        controller = CONTROL.Controller(self.args)
        events = []
        controller.event = lambda *args, **kwargs: events.append((args, kwargs))

        unsafe = active.parent / "unexpected"
        unsafe.symlink_to(active)
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "active-run state is unsafe",
        ):
            controller.reclaim_orphaned_execution_cells([claim])
        self.assertTrue(all((cells / f"cell-{number}").exists() for number in range(1, 6)))
        unsafe.unlink()

        lock_path = cells / ".dispatch-admission.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        CONTROL.fcntl.flock(descriptor, CONTROL.fcntl.LOCK_EX)
        try:
            controller.reclaim_orphaned_execution_cells([claim])
            self.assertTrue(all((cells / f"cell-{number}").exists() for number in range(1, 6)))
        finally:
            CONTROL.fcntl.flock(descriptor, CONTROL.fcntl.LOCK_UN)
            os.close(descriptor)

        controller.reclaim_orphaned_execution_cells([claim])
        self.assertFalse((cells / "cell-1").exists())
        self.assertTrue((cells / "cell-2").exists())
        self.assertTrue((cells / "cell-3").exists())
        self.assertTrue((cells / "cell-4").exists())
        self.assertTrue((cells / "cell-5").exists())
        (active / "owner").unlink()
        active.rmdir()
        controller.reclaim_orphaned_execution_cells([claim])
        controller.reclaim_orphaned_execution_cells([claim])
        self.assertFalse((cells / "cell-4").exists())
        self.assertFalse((cells / "cell-5").exists())
        self.assertTrue(run(
            "git", "show-ref", "--verify", "refs/heads/ticket/T-110",
            cwd=self.product,
        ).stdout)
        reclaimed = [item for item in events if item[0][0] == "execution_cell_reclaimed"]
        self.assertEqual(
            [(item[0][1], item[1]["worktree"]) for item in reclaimed],
            [
                ("T-110", str(cells / "cell-1")),
                ("T-113", str(cells / "cell-4")),
                ("", str(cells / "cell-5")),
            ],
        )

    def test_controller_derives_worktree_root_without_new_launcher_args(
        self,
    ) -> None:
        launcher = (
            ROOT / "scripts/factory-launch"
        ).read_text(encoding="utf-8")
        reconcile = launcher.split("  reconcile)", 1)[1].split("\n    ;;", 1)[0]
        self.assertNotIn("--worktree-root", reconcile)
        self.assertNotIn("CONTROLLER_WORKTREE_ROOT", reconcile)

        with patch.dict(os.environ, {"FACTORY_RELEASE_PATH": ""}):
            self.assertIsNone(CONTROL.Controller(self.args).worktree_root)
        home = self.root / "home"
        release = home / ".factory/kits/releases" / ("b" * 40)
        release.mkdir(parents=True)
        self.args.release_path = release
        with patch.dict(
            os.environ, {
                "FACTORY_RELEASE_PATH": str(release),
                "HOME": str(home),
            },
        ):
            self.assertEqual(
                CONTROL.Controller(self.args).worktree_root,
                home / ".factory/worktrees/relay",
            )
            explicit = self.root / "explicit"
            self.args.worktree_root = explicit
            self.assertEqual(CONTROL.Controller(self.args).worktree_root, explicit)
            self.args.worktree_root = None
            os.environ["FACTORY_RELEASE_PATH"] = str(release) + ".moved"
            self.assertIsNone(CONTROL.Controller(self.args).worktree_root)

        foreign = self.root / "foreign/releases" / ("c" * 40)
        foreign.mkdir(parents=True)
        self.args.release_path = foreign
        with patch.dict(os.environ, {"FACTORY_RELEASE_PATH": str(foreign)}):
            self.assertIsNone(CONTROL.Controller(self.args).worktree_root)

        private_tmp = Path("/private/tmp")
        if private_tmp.is_dir():
            with tempfile.TemporaryDirectory(
                prefix="nysa-sf-qualification.", dir=private_tmp,
            ) as directory:
                lane = Path(directory).resolve()
                release = lane / "releases" / ("d" * 40)
                release.mkdir(parents=True)
                self.args.release_path = release
                with patch.dict(os.environ, {"FACTORY_RELEASE_PATH": str(release)}):
                    self.assertEqual(
                        CONTROL.Controller(self.args).worktree_root,
                        lane / "worktrees/relay",
                    )

    def test_qualification_parks_checkpoint_under_durable_controller_state(self) -> None:
        run = lambda *command, cwd=None: subprocess.run(
            command, cwd=cwd, text=True, capture_output=True, check=True
        )
        (self.product / "factory/QUALIFICATION.json").write_text(json.dumps({
            "budget_usd": "100.000000",
            "capacity": 4,
            "contract_version": "1.8.0",
            "factory_sha": "a" * 40,
            "generation": 1,
            "per_run_budget_usd": "2.000000",
            "per_ticket_budget_usd": "25.000000",
            "schema": CONTROL.QUALIFICATION_SCHEMA,
            "target_done": 4,
            "tickets": ["T-110", "T-111", "T-112", "T-113"],
        }), encoding="utf-8")
        run("git", "init", "-q", "-b", "main", cwd=self.product)
        run("git", "config", "user.name", "Test", cwd=self.product)
        run("git", "config", "user.email", "test@example.invalid", cwd=self.product)
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "seed", cwd=self.product)
        run("git", "branch", "ticket/T-110", cwd=self.product)
        scratch = self.root / "scratch"
        scratch.mkdir(mode=0o700)
        cell = scratch / "cell-1"
        run(
            "git", "worktree", "add", "-q", str(cell), "ticket/T-110",
            cwd=self.product,
        )
        controller = CONTROL.Controller(self.args)
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        controller.remote_passport_valid = lambda _claim: True
        controller.json_call = lambda *_args, **_kwargs: {}
        controller.event = lambda *_args, **_kwargs: None

        self.assertTrue(controller.park_claim(claim))
        self.assertEqual(
            claim["worktree"], str(self.state / "parked/T-110")
        )
        controller.ensure_execution_cell(claim)
        self.assertEqual(claim["worktree"], str(self.state / "cells/cell-1"))

    def test_approval_attestation_precedes_h2_merge_lease_and_keeps_it(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        stages = iter((
            state_transition(
                "AWAIT-OPERATOR operator approval observed", "b" * 64
            ),
            state_transition(
                "AWAIT-MERGE approval attested; "
                "protected auto-merge request pending",
                "c" * 64,
            ),
        ))
        heads = iter(("d" * 40, "e" * 40))
        calls = []
        controller.renew = lambda _claim: None
        controller.finish_pending_run = lambda _claim: True
        controller.refresh_dependency_tracking = lambda _claim: True
        controller.migrate_passport = lambda *_args: calls.append("passport")
        controller.event = lambda *_args, **_kwargs: None
        controller.withdraw_publication = lambda *_args: None

        def json_call(*arguments, **_kwargs):
            if arguments[0] == "state-machine":
                return next(stages)
            if arguments[0] == "ticket-pr":
                return {
                    "head": next(heads), "pr_number": 24, "status": "ready",
                }
            if arguments[0] == "ticket-attest":
                calls.append(arguments)
                return (
                    {
                        "action": "approval-attested",
                        "auto_merge": False,
                        "head": "e" * 40,
                        "pr_number": 24,
                    }
                    if "--attest-only" in arguments
                    else {
                        "action": "approval",
                        "auto_merge": True,
                        "head": "e" * 40,
                        "pr_number": 24,
                    }
                )
            raise AssertionError(arguments)

        controller.json_call = json_call
        controller.publication_ready = lambda item, _receipt, head: (
            calls.append(("publication", head)),
            item.update(publication_lease="f" * 64),
            True,
        )[-1]
        self.assertEqual(
            controller.reconcile_ticket(claim)["status"], "progressed"
        )
        self.assertIn("--attest-only", calls[0])
        self.assertEqual(
            controller.reconcile_ticket(claim)["status"], "progressed"
        )
        self.assertEqual(calls[2], ("publication", "e" * 40))
        self.assertNotIn("--attest-only", calls[3])
        self.assertEqual(claim["publication_lease"], "f" * 64)

    def test_failed_awaiting_approval_check_reopens_publication_repair(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        (cell / "factory/route-plans").mkdir(parents=True)
        (cell / "factory/route-plans/T-110.json").write_text("{}\n")
        claim = {
            "branch": "ticket/T-110", "lease": "a" * 64,
            "priority": "normal", "publication_lease": "",
            "receipt": "", "role": "", "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed", "ticket": "T-110", "worktree": str(cell),
        }
        calls = []
        controller.renew = lambda _claim: None
        controller.finish_pending_run = lambda _claim: True
        controller.refresh_dependency_tracking = lambda _claim: True
        controller.withdraw_publication = lambda *_args: None
        controller.retry_ci = lambda *_args: False
        controller.publication_repair = lambda _claim, receipt, pr: calls.append(
            (receipt, pr["pr_number"])
        )
        controller.json_call = lambda *arguments, **_kwargs: (
            state_transition(
                "AWAIT-OPERATOR operator approval observed", "b" * 64
            )
            if arguments[0] == "state-machine"
            else {"pr_number": 24, "status": "failed"}
        )

        self.assertEqual(
            controller.reconcile_ticket(claim)["status"], "progressed"
        )
        self.assertEqual(calls, [("b" * 64, 24)])

    def test_stale_approval_recovers_only_from_exact_prepublication_boundary(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "parked/T-110"
        (cell / "factory/route-plans").mkdir(parents=True)
        (cell / "factory/route-plans/T-110.json").write_text(
            "{}\n", encoding="utf-8"
        )
        bundle = cell / "factory/attestations/T-110/bundle.json"
        bundle.parent.mkdir(parents=True)
        bundle.write_text('{"schema":"bundle"}\n', encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(cell)], check=True)
        subprocess.run(["git", "-C", str(cell), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(cell), "-c", "user.name=Factory",
            "-c", "user.email=factory@example.invalid", "commit", "-qm", "bundle",
        ], check=True)
        head = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        (self.state / "passports").mkdir(mode=0o700)
        CONTROL.write(self.state / "passports/T-110.json", {
            "branch": "ticket/T-110", "factory_sha": self.release.name,
            "head_sha": head, "passport_sha256": "c" * 64,
            "publication_state": "validating", "ticket": "T-110",
        })
        claim = {
            "branch": "ticket/T-110", "lease": "a" * 64,
            "parked": True, "priority": "normal", "publication_lease": "",
            "receipt": "", "role": "", "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed", "ticket": "T-110", "worktree": str(cell),
        }
        controller.renew = lambda _claim: None
        controller.finish_pending_run = lambda _claim: True
        controller.refresh_dependency_tracking = lambda _claim: True
        controller.withdraw_publication = lambda *_args: None
        controller.release_ticket_lease = lambda item: item.update(
            lease_released=True
        )
        controller.role_active = lambda _claim: False

        def json_call(*arguments, **_kwargs):
            if arguments[0] == "state-machine":
                return state_transition(
                    "AWAIT-OPERATOR operator approval observed", "b" * 64
                )
            if arguments[0] == "ticket-pr":
                return {"head": head, "pr_number": 24, "status": "ready"}
            if arguments[0] == "ticket-attest":
                raise CONTROL.ControllerError(
                    "ticket-attest: stale_operator_approval: operator approval "
                    "is not newer than the bundle attestation"
                )
            raise AssertionError(arguments)

        controller.json_call = json_call
        self.assertEqual(controller.reconcile_ticket(claim)["status"], "error")
        marker = controller.prepublication_retry_path("T-110")
        self.assertTrue(marker.exists())
        self.assertEqual(claim["blocked_reason"], "controller-error")

        controller.ticket_release_current = lambda _claim: True
        controller.remote_passport_valid = lambda _claim: True
        leases = []
        controller.ensure_lease = lambda _claim, label: leases.append(label)
        controller.recover_prepublication_attestations([claim])
        controller.recover_prepublication_attestations([claim])

        self.assertEqual(claim["status"], "claimed")
        self.assertNotIn("blocked_reason", claim)
        self.assertFalse(marker.exists())
        self.assertEqual(leases, ["prepublication-attestation"])
        events = [
            CONTROL.read(path) for path in controller.events.glob("*.json")
            if CONTROL.read(path).get("event")
            == "prepublication_attestation_recovered"
        ]
        self.assertEqual(len(events), 1)

    def test_pushed_prepublication_attestations_recover_without_role_replay(
        self,
    ) -> None:
        cases = (
            ("T-110", "Review", "validating", "bundle"),
            (
                "T-111", "Awaiting Approval", "merge-pending",
                "approval",
            ),
        )
        for ticket, state, publication, recovery in cases:
            with self.subTest(recovery=recovery):
                controller = CONTROL.Controller(self.args)
                old_head = ("b" if ticket == "T-110" else "c") * 40
                new_head = ("d" if ticket == "T-110" else "e") * 40
                digest = self.operator_passport(
                    ticket, state, "validating", head_sha=old_head,
                )
                claim = {
                    "blocked_reason": (
                        "external-unavailable"
                        if recovery == "bundle" else "controller-error"
                    ),
                    "branch": f"ticket/{ticket}", "lease": "",
                    "priority": "normal", "publication_lease": "",
                    "receipt": "", "role": "",
                    "schema": CONTROL.CLAIM_SCHEMA, "status": "blocked",
                    "ticket": ticket, "worktree": str(self.root / ticket),
                }
                CONTROL.write(controller.reconciliation_marker(ticket), {
                    "branch": claim["branch"],
                    "factory_sha": self.release.name,
                    "head_sha": old_head,
                    "passport_sha256": digest,
                    "run_snapshot_sha256": controller.ticket_run_snapshot(ticket),
                    "schema": (
                        "nysa.software-factory.reconciliation-boundary/v1"
                    ),
                    "ticket": ticket,
                })
                calls = []
                controller.role_active = lambda _claim: False
                controller.ticket_release_current = lambda _claim: True
                controller.remote_cell_head_status = lambda _claim: (
                    ("pushed", new_head, new_head)
                    if recovery == "bundle" else
                    ("resume_commit_not_pushed", new_head, old_head)
                )
                controller.cell_git = lambda *_args: subprocess.CompletedProcess(
                    _args, 0, stdout="", stderr="",
                )
                controller.transition_receipt = lambda *_args, **_kwargs: {
                    "receipt_sha256": "f" * 64,
                }
                controller.ensure_lease = lambda item, label: (
                    calls.append(("lease", label)), item.update(lease="a" * 64)
                )
                controller.migrate_passport = (
                    lambda _claim, target, expected_head="": (
                        calls.append(("passport", target, expected_head))
                        or {"status": "ok"}
                    )
                )
                controller.remote_passport_valid = lambda _claim: True
                controller.save_claim = lambda _claim: calls.append("save")
                controller.event_once = (
                    lambda name, _ticket, **details:
                    calls.append(("event", name, details["recovery"]))
                )
                controller.json_call = lambda *arguments, **_kwargs: (
                    calls.append(("attest", arguments))
                    or {
                        "action": (
                            "approval-attested"
                            if recovery == "approval" else "bundle"
                        ),
                        "head": new_head,
                    }
                )
                self.assertTrue(
                    controller.recover_pushed_prepublication_attestation(
                        claim,
                    )
                )

                self.assertEqual(claim["status"], "claimed")
                self.assertNotIn("blocked_reason", claim)
                self.assertFalse(
                    controller.reconciliation_marker(ticket).exists()
                )
                self.assertIn(
                    ("passport", publication, new_head), calls,
                )
                self.assertIn(
                    (
                        "event",
                        "pushed_prepublication_attestation_recovered",
                        recovery,
                    ),
                    calls,
                )

    def test_projected_approval_stage_still_requests_exact_auto_merge(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        receipt = "b" * 64
        head = "d" * 40
        calls = []
        controller.renew = lambda _claim: None
        controller.finish_pending_run = lambda _claim: True
        controller.refresh_dependency_tracking = lambda _claim: True
        controller.ticket_merged = lambda _claim: False
        controller.withdraw_publication = lambda *_args: None
        controller.publication_ready = lambda item, _receipt, exact_head: (
            calls.append(("publication", exact_head)),
            item.update(publication_lease="f" * 64),
            True,
        )[-1]
        controller.migrate_passport = lambda *_args: calls.append("passport")
        controller.event = lambda name, *_args, **_kwargs: calls.append(name)

        def json_call(*arguments, **_kwargs):
            if arguments[0] == "state-machine":
                return state_transition(
                    "AWAIT-MERGE protected auto-merge requested; "
                    "await merge and closeout",
                    receipt,
                )
            if arguments[0] == "ticket-pr":
                return {
                    "head": head, "pr_number": 24, "status": "ready",
                }
            if arguments[0] == "ticket-attest":
                calls.append(arguments)
                return {
                    "action": "approval", "auto_merge": True,
                    "head": head, "pr_number": 24,
                }
            raise AssertionError(arguments)

        controller.json_call = json_call
        self.assertEqual(
            controller.reconcile_ticket(claim),
            {"status": "waiting", "ticket": "T-110"},
        )
        self.assertEqual(calls[0], ("publication", head))
        self.assertEqual(calls[1][0], "ticket-attest")
        self.assertIn("passport", calls)
        self.assertIn("protected_auto_merge_requested", calls)
        self.assertEqual(claim["publication_lease"], "f" * 64)

    def test_auto_merge_race_rechecks_merged_before_open_pr_failure(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "f" * 64,
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        merged = iter((False, True))
        calls = []
        controller.renew = lambda _claim: None
        controller.finish_pending_run = lambda _claim: True
        controller.refresh_dependency_tracking = lambda _claim: True
        controller.ticket_merged = lambda _claim: next(merged)
        controller.ticket_pr = lambda *_args: (_ for _ in ()).throw(
            CONTROL.ControllerError("ticket-pr: ticket PR is not open")
        )
        controller.release_publication = lambda _claim: calls.append("release")
        controller.migrate_passport = lambda *_args: calls.append("passport")
        controller.closeout = lambda _claim: calls.append("closeout") or False
        controller.withdraw_publication = lambda *_args: None
        controller.json_call = lambda *arguments, **_kwargs: (
            state_transition(
                "AWAIT-MERGE protected auto-merge requested; "
                "await merge and closeout"
            )
            if arguments[0] == "state-machine"
            else (_ for _ in ()).throw(AssertionError(arguments))
        )

        self.assertEqual(
            controller.reconcile_ticket(claim),
            {"status": "progressed", "ticket": "T-110"},
        )
        self.assertEqual(calls, ["release", "passport", "closeout"])

    def test_launcher_authorizes_requested_stage_publication_recovery(self) -> None:
        launcher = (
            ROOT / "scripts/factory-launch"
        ).read_text(encoding="utf-8")
        phase_two = launcher.index(
            'die "transition receipt does not authorize protected auto-merge"'
        )
        guard = launcher[phase_two - 500:phase_two]
        self.assertIn(
            '"$TRANSITION_STAGE" == AWAIT-MERGE\\ approval\\ attested*',
            guard,
        )
        self.assertIn(
            '"AWAIT-MERGE protected auto-merge requested; '
            'await merge and closeout"',
            guard,
        )
        refresh = launcher.index(
            'die "transition receipt does not authorize refresh"'
        )
        refresh_guard = launcher[refresh - 500:refresh]
        self.assertIn('"$TRANSITION_STAGE" == "RUN reviewer"', refresh_guard)
        self.assertIn('"$TRANSITION_STAGE" == "RUN narrator"', refresh_guard)
        self.assertNotIn('"$TRANSITION_STAGE" == RUN*', refresh_guard)

    def test_launcher_ticket_parking_requires_issue_and_named_release(self) -> None:
        launcher = (
            ROOT / "scripts/factory-launch"
        ).read_text(encoding="utf-8")
        contract = json.loads(
            (ROOT / "factory-contract.json").read_text()
        )["launcher"]["commands"]["ticket-control"]
        self.assertIn('"$4" == "--issue"', launcher)
        self.assertIn('"$4" == "--factory-sha"', launcher)
        self.assertIn('"$1" == "retry-preview"', launcher)
        self.assertIn('"$1" == "authorize-round"', launcher)
        self.assertIn('"$1" == "contract-repair"', launcher)
        self.assertIn('"$1" == "reviewer-void"', launcher)
        self.assertIn('"${11}" == "--approve-hash"', launcher)
        self.assertEqual(contract["grammars"], [
            "pause --ticket <T-NNN> --issue "
            "<software-factory-issue-url> --json",
            "resume --ticket <T-NNN> --factory-sha <FULL_SHA> --json",
            "retry-preview --ticket <T-NNN> --operator-id <ID> --json",
            "authorize-round plan --ticket <T-NNN> --role "
            "<planner|spec-linter|test-author|builder|narrator> "
            "--round <N> --operator-id <ID> --json",
            "authorize-round apply --ticket <T-NNN> --role "
            "<planner|spec-linter|test-author|builder|narrator> "
            "--round <N> --operator-id <ID> --approve-hash <HASH> --json",
            "contract-repair plan --ticket <T-NNN> --role "
            "<planner|spec-linter|test-author|builder> "
            "--operator-id <ID> --json",
            "contract-repair apply --ticket <T-NNN> --role "
            "<planner|spec-linter|test-author|builder> "
            "--operator-id <ID> --approve-hash <HASH> --json",
            "reviewer-void plan --ticket <T-NNN> --run <N> "
            "--operator-id <ID> --json",
            "reviewer-void apply --ticket <T-NNN> --run <N> "
            "--operator-id <ID> --approve-hash <HASH> --json",
        ])

    def test_dependency_refresh_race_waits_then_migrates_exact_base(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        (cell / "factory/tickets").mkdir()
        (cell / "factory/tickets/T-110.md").write_text(
            "# T-110\n\nState: Building\nDepends-On: T-094\n",
            encoding="utf-8",
        )
        old = "b" * 40
        protected = "e" * 40
        refreshed = "f" * 40
        stage = (
            "REFUSE dependency refresh required; "
            f"dependencies=T-094; protected-main={protected}"
        )
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        receipt = self.operator_transition("T-110", stage)
        results = iter((
            {
                "action": "dependency-wait",
                "expected_protected_head": protected,
                "observed_protected_head": "1" * 40,
            },
            {
                "action": "dependency-publication-refresh",
                "attestation": {
                    "old_head": old,
                    "base_head": protected,
                },
                "dependencies": ["T-094"],
                "dependency_terminals": [{
                    "ticket": "T-094", "terminal_sha256": "a" * 64,
                }],
                "head": refreshed,
            },
        ))
        migrations = []
        events = []
        controller.renew = lambda _claim: None
        controller.finish_pending_run = lambda _claim: True
        controller.refresh_dependency_tracking = lambda _claim: True
        controller.withdraw_publication = lambda _claim: None
        def migrate_passport(*_args):
            migrations.append("passport")
            self.operator_passport(
                "T-110", "Review", "validating", head_sha=refreshed,
            )

        controller.migrate_passport = migrate_passport
        controller.event = lambda name, *_args, **kwargs: events.append(
            (name, kwargs)
        )

        state_machine_calls = 0
        ticket_attest_calls = 0

        def json_call(*arguments, **_kwargs):
            nonlocal state_machine_calls, ticket_attest_calls
            if arguments[0] == "state-machine":
                state_machine_calls += 1
                if state_machine_calls == 1:
                    return state_transition(stage, receipt)
                if state_machine_calls == 2:
                    return state_transition(
                        "AWAIT_DEPENDENCY T-094", "c" * 64,
                    )
                raise AssertionError("unexpected extra stage resolution")
            if arguments[0] == "ticket-attest":
                ticket_attest_calls += 1
                self.assertEqual(
                    arguments[arguments.index("--action") + 1],
                    (
                        "dependency-refresh-replay"
                        if ticket_attest_calls >= 2
                        else "dependency-refresh"
                    ),
                )
                self.assertEqual(
                    "--receipt" in arguments, ticket_attest_calls == 1,
                )
                if ticket_attest_calls == 2:
                    raise SystemExit(
                        "simulated crash after replay helper"
                    )
                return next(results)
            raise AssertionError(arguments)

        controller.json_call = json_call

        observed_heads = iter((old, *([refreshed] * 7)))

        def run(command, **_kwargs):
            if "log" in command:
                return CONTROL.subprocess.CompletedProcess(
                    command, 0, refreshed + "\n", "",
                )
            if "rev-parse" in command:
                return CONTROL.subprocess.CompletedProcess(
                    command, 0, next(observed_heads) + "\n", "",
                )
            if "merge-base" in command:
                return CONTROL.subprocess.CompletedProcess(command, 0, "", "")
            raise AssertionError(command)

        with patch.object(CONTROL.subprocess, "run", side_effect=run):
            self.assertEqual(
                controller.reconcile_ticket(claim)["status"], "waiting"
            )
            self.assertEqual(migrations, [])
            refresh = cell / "factory/attestations/T-110/refresh.json"
            refresh.parent.mkdir(parents=True)
            refresh.write_text("{}\n", encoding="utf-8")
            receipt = self.operator_transition("T-110", stage, consumed=True)
            self.operator_passport(
                "T-110", "Review", "validating", head_sha=old,
            )
            self.operator_passport(
                "T-110", "Review", "validating", head_sha=old,
                factory_sha="9" * 40,
            )
            with self.assertRaisesRegex(
                CONTROL.ControllerError, "replay passport is invalid",
            ):
                controller.dependency_publication_replay_transition(claim)
            self.operator_passport(
                "T-110", "Review", "validating", head_sha=old,
                branch="ticket/T-999",
            )
            with self.assertRaisesRegex(
                CONTROL.ControllerError, "replay passport is invalid",
            ):
                controller.dependency_publication_replay_transition(claim)
            self.operator_passport(
                "T-110", "Review", "validating", head_sha=old,
            )
            claim["status"] = "claimed"
            with self.assertRaisesRegex(
                SystemExit, "crash after replay helper",
            ):
                controller.reconcile_ticket(claim)
            self.assertEqual(migrations, [])
            claim["status"] = "claimed"
            self.assertEqual(
                controller.reconcile_ticket(claim)["status"], "progressed"
            )
            claim["status"] = "claimed"
            self.assertEqual(
                controller.reconcile_ticket(claim)["status"], "waiting"
            )
        self.assertEqual(state_machine_calls, 2)
        self.assertEqual(ticket_attest_calls, 3)
        self.assertEqual(migrations, ["passport"])
        self.assertEqual(
            sum(
                name == "dependency_publication_evidence_retired"
                for name, _details in events
            ),
            1,
        )

    def test_dependency_test_conflict_routes_without_generic_block(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        (cell / "factory/tickets").mkdir()
        (cell / "factory/tickets/T-110.md").write_text(
            "# T-110\n\nState: Building\nDepends-On: T-094\n",
            encoding="utf-8",
        )
        old = "d" * 40
        protected = "e" * 40
        refreshed = "f" * 40
        stage = (
            "REFUSE dependency refresh required; "
            f"dependencies=T-094; protected-main={protected}"
        )
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        receipt = self.operator_transition("T-110", stage, head_sha=old)
        migrations = []
        events = []
        controller.renew = lambda _claim: None
        controller.finish_pending_run = lambda _claim: True
        controller.refresh_dependency_tracking = lambda _claim: True
        controller.withdraw_publication = lambda _claim: None
        controller.migrate_passport = lambda *_args: migrations.append("passport")
        controller.event = lambda name, *_args, **kwargs: events.append(
            (name, kwargs)
        )

        def json_call(*arguments, **_kwargs):
            if arguments[0] == "state-machine":
                return state_transition(stage, receipt)
            if arguments[0] == "ticket-attest":
                return {
                    "action": "dependency-conflict-refresh",
                    "attestation": {
                        "conflicts": [{"path": "tests/conflict.test.ts"}],
                        "old_head": old,
                        "protected_head": protected,
                        "repair_owner": "test-author",
                    },
                    "head": refreshed,
                }
            raise AssertionError(arguments)

        controller.json_call = json_call

        def run(command, **_kwargs):
            if "rev-parse" in command:
                return CONTROL.subprocess.CompletedProcess(
                    command, 0, old + "\n", "",
                )
            if "merge-base" in command:
                return CONTROL.subprocess.CompletedProcess(command, 0, "", "")
            raise AssertionError(command)

        with patch.object(CONTROL.subprocess, "run", side_effect=run):
            self.assertEqual(
                controller.reconcile_ticket(claim)["status"], "progressed"
            )
        self.assertEqual(migrations, ["passport"])
        self.assertEqual(events[-1][0], "dependency_conflict_routed")
        self.assertEqual(events[-1][1]["repair_owner"], "test-author")
        self.assertEqual(
            events[-1][1]["conflict_paths"], ["tests/conflict.test.ts"]
        )
        self.assertEqual(claim["status"], "claimed")

    def test_merged_publication_closes_before_dependency_refresh(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "b" * 64,
            "receipt": "c" * 64,
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        calls = []
        controller.ensure_lease = lambda *_args: None
        controller.finish_pending_run = lambda _claim: True
        controller.ticket_merged = lambda _claim: True
        controller.release_publication = lambda _claim: calls.append(
            "release"
        )
        controller.migrate_passport = lambda *_args: calls.append("passport")
        controller.closeout = lambda _claim: calls.append("closeout") or False
        controller.refresh_dependency_tracking = lambda _claim: (
            (_ for _ in ()).throw(
                AssertionError("merged ticket refreshed dependencies")
            )
        )
        controller.json_call = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(
                AssertionError("merged ticket entered state machine")
            )
        )
        self.assertEqual(
            controller.reconcile_ticket(claim),
            {"status": "waiting", "ticket": "T-110"},
        )
        self.assertEqual(calls, ["release", "passport", "closeout"])

    def test_recovered_merged_passport_closes_without_publication_lease(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        (self.state / "passports").mkdir()
        passport = self.state / "passports/T-110.json"
        passport.write_text(
            json.dumps({"publication_state": "merged"}), encoding="utf-8"
        )
        passport.chmod(0o600)
        calls = []
        controller.ensure_lease = lambda *_args: None
        controller.finish_pending_run = lambda _claim: True
        controller.ticket_merged = lambda _claim: True
        controller.migrate_passport = lambda *_args: calls.append("passport")
        controller.closeout = lambda _claim: calls.append("closeout") or False
        controller.refresh_dependency_tracking = lambda _claim: (
            (_ for _ in ()).throw(
                AssertionError("merged recovery refreshed dependencies")
            )
        )
        controller.json_call = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(
                AssertionError("merged recovery entered state machine")
            )
        )
        self.assertEqual(
            controller.reconcile_ticket(claim),
            {"status": "waiting", "ticket": "T-110"},
        )
        self.assertEqual(calls, ["passport", "closeout"])

    def test_merged_closeout_attestation_completes_before_dependency_refresh(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        (self.state / "passports").mkdir()
        passport = self.state / "passports/T-110.json"
        passport.write_text(
            json.dumps({"publication_state": "merged"}), encoding="utf-8"
        )
        passport.chmod(0o600)
        calls = []
        controller.ensure_lease = lambda *_args: None
        controller.finish_pending_run = lambda _claim: True
        controller.ticket_merged = lambda _claim: True
        controller.migrate_passport = lambda *_args: calls.append("passport")
        controller.closeout = lambda _claim: calls.append("closeout") or True
        controller.refresh_dependency_tracking = lambda _claim: (
            (_ for _ in ()).throw(
                AssertionError("attested Done refreshed dependencies")
            )
        )
        controller.json_call = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(
                AssertionError("attested Done entered branch state machine")
            )
        )
        controller.event = lambda name, *_args, **_kwargs: calls.append(name)
        controller.release = lambda _claim: calls.append("release")
        self.assertEqual(
            controller.reconcile_ticket(claim),
            {"status": "complete", "ticket": "T-110"},
        )
        self.assertEqual(
            calls, ["passport", "closeout", "ticket_complete", "release"]
        )

    def test_run_wrapper_renews_lease_before_provider_queue(self) -> None:
        source = (ROOT / "scripts/run-agent.sh").read_text(encoding="utf-8")
        required = source.index(
            'if ! factory_dispatch_require_lease "$REPO_ROOT" "$TICKET"'
        )
        heartbeat = source.index("start_lease_heartbeat", required)
        provider = source.index(
            "# --- resolve one backend before reservation", required
        )
        self.assertLess(required, heartbeat)
        self.assertLess(heartbeat, provider)

    def test_missing_claim_recovers_only_from_current_passport_cell(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {"tickets": ["T-110"]}
        cell = self.root / "cell-1"
        cell.mkdir()
        (self.state / "passports").mkdir(mode=0o700)
        CONTROL.write(
            self.state / "passports/T-110.json",
            {
                "branch": "ticket/T-110",
                "current_state": "Review",
                "ticket": "T-110",
            },
        )
        calls = []
        controller.ticket_release_current = lambda _claim: True
        controller.migrate_passport = lambda *_args: calls.append("passport")
        controller.event = lambda name, *_args, **_kwargs: calls.append(name)
        controller.json_call = lambda *args, **_kwargs: (
            {
                "lease_id": "a" * 64,
                "schema_version": 1,
                "ticket": "T-110",
            }
            if args[0] == "claim"
            else {}
        )
        with patch("subprocess.run") as run:
            run.return_value.stdout = (
                f"worktree {cell}\n"
                "HEAD deadbeef\n"
                "branch refs/heads/ticket/T-110\n\n"
            )
            claims = []
            controller.recover_missing_passport_claims(claims)

        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["lease"], "a" * 64)
        self.assertEqual(claims[0]["worktree"], str(cell))
        self.assertEqual(calls, ["passport", "missing_claim_recovered"])
        self.assertEqual(
            CONTROL.read(controller.claim_path("T-110"))["status"],
            "claimed",
        )

    def test_done_product_ticket_is_not_recovered_from_passport(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {"tickets": ["T-110"]}
        (self.product / "factory/tickets").mkdir()
        (self.product / "factory/tickets/T-110.md").write_text(
            "State: Done\n", encoding="utf-8"
        )
        (self.state / "passports").mkdir(mode=0o700)
        CONTROL.write(
            self.state / "passports/T-110.json",
            {
                "branch": "ticket/T-110",
                "current_state": "Approved",
                "ticket": "T-110",
            },
        )
        claims = []
        with patch.object(CONTROL.subprocess, "run") as run:
            run.return_value.returncode = 1
            controller.recover_missing_passport_claims(claims)
        self.assertEqual(claims, [])
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][3], "show")

    def test_done_product_ticket_prunes_existing_claim_before_recovery(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.protected_main_head = lambda: "f" * 40
        (self.product / "factory/tickets").mkdir()
        (self.product / "factory/tickets/T-110.md").write_text(
            "State: Done\n", encoding="utf-8"
        )
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        controller.save_claim(claim)
        controller.load_claims = lambda: (
            [claim] if controller.claim_path("T-110").exists() else []
        )
        calls = []
        controller.ensure_lease = lambda *_args: calls.append("ensure")

        def release(item):
            calls.append("release")
            controller.claim_path(item["ticket"]).unlink()

        controller.release = release
        controller.recover_missing_passport_claims = (
            lambda claims: calls.append(("recover", len(claims)))
        )
        controller.recover_each = lambda *_args, **_kwargs: None
        controller.event = lambda *_args, **_kwargs: None
        controller.claim_new = lambda claims, *_args: claims
        controller.pin_routes = lambda _claims: []
        result = controller.reconcile()
        self.assertEqual(result["active"], 0)
        self.assertFalse(controller.claim_path("T-110").exists())
        self.assertEqual(calls[:3], ["ensure", "release", ("recover", 0)])

    def test_canceled_product_ticket_retires_claim_without_reacquiring(self) -> None:
        controller = CONTROL.Controller(self.args)
        (self.product / "factory/tickets").mkdir()
        (self.product / "factory/tickets/T-110.md").write_text(
            "State: Canceled\n", encoding="utf-8"
        )
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "b" * 64,
            "receipt": "c" * 64,
            "role": "planner",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(self.root / "parked/T-110"),
        }
        controller.save_claim(claim)
        controller.load_claims = lambda: (
            [claim] if controller.claim_path("T-110").exists() else []
        )
        controller.protected_main_head = lambda: "f" * 40
        controller.product_ticket_canceled = lambda _ticket, _main=None: True
        calls = []
        controller.role_active = lambda _claim: False
        controller.withdraw_publication = lambda item: (
            calls.append("withdraw"), item.update(publication_lease="")
        )[-1]
        controller.release_ticket_lease = lambda item: (
            calls.append(("release", item["lease"])),
            item.update(lease_released=True),
            controller.save_claim(item),
        )[-1]
        controller.event_once = lambda name, ticket, **details: calls.append(
            (name, ticket, details)
        )
        controller.recover_missing_passport_claims = (
            lambda claims: calls.append(("recover", len(claims)))
        )
        controller.recover_each = lambda *_args, **_kwargs: None
        controller.event = lambda *_args, **_kwargs: None
        controller.claim_new = lambda claims, *_args: claims
        controller.pin_routes = lambda _claims: []

        result = controller.reconcile()

        self.assertEqual(result["active"], 0)
        self.assertFalse(controller.claim_path("T-110").exists())
        self.assertEqual(
            calls,
            [
                "withdraw",
                ("release", "a" * 64),
                ("ticket_retired", "T-110", {"reason": "canceled"}),
                ("recover", 0),
                ("recover", 0),
            ],
        )

    def test_canceled_ticket_retirement_waits_for_active_role(self) -> None:
        controller = CONTROL.Controller(self.args)
        (self.product / "factory/tickets").mkdir()
        (self.product / "factory/tickets/T-110.md").write_text(
            "State: Canceled\n", encoding="utf-8"
        )
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "planner",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(self.root / "parked/T-110"),
        }
        controller.save_claim(claim)
        controller.load_claims = lambda: (
            [claim] if controller.claim_path("T-110").exists() else []
        )
        controller.protected_main_head = lambda: "f" * 40
        controller.product_ticket_canceled = lambda _ticket, _main=None: True
        active = [True]
        controller.role_active = lambda _claim: active.pop(0) if active else False
        entered_loop = False
        recovered_after_drain = []

        def recover_each(claims, *_args, **_kwargs):
            if entered_loop:
                recovered_after_drain.extend(item["ticket"] for item in claims)

        def event(name, *_args, **_kwargs):
            nonlocal entered_loop
            if name == "controller_started":
                entered_loop = True

        released = []
        controller.withdraw_publication = lambda _claim: None
        controller.release_ticket_lease = lambda item: (
            released.append(item["ticket"]), item.update(lease_released=True),
            controller.save_claim(item),
        )[-1]
        controller.event_once = lambda *_args, **_kwargs: None
        controller.event = event
        controller.recover_operator_action_events = lambda *_args: None
        controller.record_qualification_done_targets = lambda: None
        controller.recover_missing_passport_claims = lambda claims: (
            recovered_after_drain.extend(
                item["ticket"] for item in claims
            ) if entered_loop else None
        )
        controller.recover_terminal_requests = lambda *_args: None
        controller.recover_each = recover_each
        controller.claim_new = lambda claims, *_args: claims
        controller.clear_admission_failure = lambda: None
        controller.pin_routes = lambda _claims: []

        result = controller.reconcile()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(released, ["T-110"])
        self.assertEqual(recovered_after_drain, [])
        self.assertFalse(controller.claim_path("T-110").exists())

    def test_qualification_never_treats_canceled_as_done(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {"tickets": ["T-110"]}
        (self.product / "factory/tickets").mkdir()
        (self.product / "factory/tickets/T-110.md").write_text(
            "State: Canceled\n", encoding="utf-8"
        )
        claim = {"ticket": "T-110"}
        controller.role_active = lambda _claim: False

        self.assertFalse(controller.product_ticket_canceled("T-110"))
        self.assertEqual(controller.retire_canceled_claims([claim]), [claim])

    def test_canceled_retirement_refreshes_current_protected_main(self) -> None:
        controller = CONTROL.Controller(self.args)
        remote = self.root / "origin.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", str(remote)],
            check=True,
        )
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.product)], check=True,
        )
        for key, value in (
            ("user.name", "Software Factory"),
            ("user.email", "factory@local"),
        ):
            subprocess.run(
                ["git", "-C", str(self.product), "config", key, value],
                check=True,
            )
        subprocess.run(
            ["git", "-C", str(self.product), "remote", "add", "origin", str(remote)],
            check=True,
        )
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.parent.mkdir()
        ticket.write_text("State: Ready\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.product), "add", "factory"], check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.product), "commit", "-qm", "ready"],
            check=True,
        )
        subprocess.run(
            [
                "git", "-C", str(self.product), "push", "-q", "origin", "main",
            ],
            check=True,
        )
        protected_ready = subprocess.run(
            ["git", "-C", str(self.product), "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()

        ticket.write_text("State: Canceled\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.product), "add", "factory"], check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.product), "commit", "-qm", "cancel"],
            check=True,
        )
        local_canceled = subprocess.run(
            ["git", "-C", str(self.product), "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        subprocess.run(
            [
                "git", "-C", str(self.product), "update-ref",
                "refs/remotes/origin/main", local_canceled,
            ],
            check=True,
        )

        self.assertFalse(controller.product_ticket_canceled("T-110"))
        self.assertEqual(
            subprocess.run(
                [
                    "git", "-C", str(self.product), "rev-parse",
                    "refs/remotes/origin/main",
                ],
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
            protected_ready,
        )
        subprocess.run(
            ["git", "-C", str(self.product), "push", "-q", "origin", "main"],
            check=True,
        )
        subprocess.run(
            [
                "git", "-C", str(self.product), "update-ref",
                "refs/remotes/origin/main", protected_ready,
            ],
            check=True,
        )
        self.assertTrue(controller.product_ticket_canceled("T-110"))

    def test_canceled_retirement_recovers_publication_release_save_crash(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "b" * 64,
            "receipt": "",
            "role": "planner",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(self.root / "parked/T-110"),
        }
        controller.save_claim(claim)
        controller.json_call = lambda *_args, **_kwargs: {"status": "released"}
        controller.save_claim = lambda _claim: (_ for _ in ()).throw(
            RuntimeError("crash after publication release")
        )
        with self.assertRaisesRegex(RuntimeError, "crash after publication release"):
            controller.release_publication(claim)
        self.assertEqual(len([
            path for path in controller.events.glob("*.json")
            if CONTROL.read(path).get("event") == "publication_released"
        ]), 1)

        persisted = CONTROL.read(controller.claim_path("T-110"))
        self.assertEqual(persisted["publication_lease"], "b" * 64)
        restarted = CONTROL.Controller(self.args)
        calls = []

        def json_call(*arguments, **_kwargs):
            calls.append(arguments[:2])
            if arguments[:2] == ("publication", "release"):
                raise CONTROL.ControllerError("publication lease does not match")
            if arguments[:2] == ("publication", "withdraw"):
                return {"status": "absent"}
            if arguments[0] == "release":
                return {"absent": True, "ticket": "T-110"}
            self.fail(f"unexpected command: {arguments}")

        restarted.json_call = json_call
        restarted.product_ticket_canceled = lambda _ticket, _main=None: True
        restarted.role_active = lambda _claim: False

        self.assertEqual(
            restarted.retire_canceled_claims([persisted], "f" * 40), []
        )
        self.assertEqual(
            calls,
            [
                ("publication", "release"),
                ("publication", "withdraw"),
                ("release", "--ticket"),
            ],
        )
        self.assertFalse(restarted.claim_path("T-110").exists())
        self.assertEqual(len([
            path for path in restarted.events.glob("*.json")
            if CONTROL.read(path).get("event") == "publication_released"
        ]), 1)

    def test_retired_claims_do_not_reduce_live_capacity_or_repeat_events(self) -> None:
        (self.product / "factory/PROJECT.env").write_text(
            "MAX_CONCURRENT_TICKETS=3\n", encoding="utf-8",
        )
        controller = CONTROL.Controller(self.args)
        canceled = {"T-110", "T-111"}
        claims = []
        for ticket in ("T-110", "T-111", "T-112", "T-113"):
            claim = {
                "branch": f"ticket/{ticket}",
                "lease": ticket[-1] * 64,
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "planner",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "blocked" if ticket in canceled else "running",
                "ticket": ticket,
                "worktree": str(self.root / f"parked/{ticket}"),
            }
            controller.save_claim(claim)
            claims.append(claim)
        controller.product_ticket_canceled = (
            lambda ticket, _main=None: ticket in canceled
        )
        controller.role_active = lambda _claim: False
        controller.withdraw_publication = lambda _claim: None
        controller.release_ticket_lease = lambda item: (
            item.update(lease_released=True), controller.save_claim(item),
        )[-1]

        retained = controller.retire_canceled_claims(claims, "f" * 40)
        repeated = controller.retire_canceled_claims(retained, "f" * 40)
        events = [
            CONTROL.read(path) for path in controller.events.glob("*.json")
            if CONTROL.read(path).get("event") == "ticket_retired"
        ]

        self.assertEqual([item["ticket"] for item in retained], ["T-112", "T-113"])
        self.assertEqual(repeated, retained)
        self.assertEqual(controller.capacity, 3)
        self.assertEqual(sum(map(controller.consumes_capacity, retained)), 2)
        self.assertEqual(sorted(item["ticket"] for item in events), sorted(canceled))
        self.assertFalse(any(
            controller.claim_path(ticket).exists() for ticket in canceled
        ))

    def test_canceled_retirement_isolates_absent_and_invalid_leases(self) -> None:
        controller = CONTROL.Controller(self.args)
        for ticket, lease, released in (
            ("T-110", "", None),
            ("T-111", "", True),
            ("T-112", "a" * 64, None),
            ("T-113", "not-a-lease", None),
            ("T-114", "b" * 64, None),
            ("T-115", "c" * 64, None),
            ("T-116", "d" * 64, None),
            ("T-117", "e" * 64, None),
        ):
            claim = {
                "branch": f"ticket/{ticket}",
                "lease": lease,
                "parked": True,
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "planner",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "blocked",
                "ticket": ticket,
                "worktree": str(self.root / f"parked/{ticket}"),
            }
            Path(claim["worktree"]).mkdir(parents=True)
            if released is not None:
                claim["lease_released"] = released
            controller.save_claim(claim)
        claims = controller.load_claims()
        self.assertIn("T-113", controller.invalid_transition_tickets)

        controller.product_ticket_canceled = (
            lambda ticket, _main=None: ticket != "T-114"
        )
        controller.role_active = lambda claim: claim["ticket"] == "T-115"
        withdrawn = []
        released = []

        def withdraw(claim):
            withdrawn.append(claim["ticket"])
            if claim["ticket"] == "T-117":
                raise CONTROL.ControllerError("publication unavailable")

        def release(claim):
            released.append(claim["ticket"])
            if claim["ticket"] == "T-116":
                raise CONTROL.ControllerError("lease unavailable")

        controller.withdraw_publication = withdraw
        controller.release_ticket_lease = release

        retained = controller.retire_canceled_claims(claims, "f" * 40)
        repeated = controller.retire_canceled_claims(retained, "f" * 40)
        events = [CONTROL.read(path) for path in controller.events.glob("*.json")]

        self.assertEqual(
            [claim["ticket"] for claim in retained],
            ["T-113", "T-114", "T-115", "T-116", "T-117"],
        )
        self.assertEqual(repeated, retained)
        self.assertEqual(released, ["T-112", "T-116", "T-116"])
        self.assertEqual(
            withdrawn,
            [
                "T-110", "T-111", "T-112", "T-113", "T-116", "T-117",
                "T-113", "T-116", "T-117",
            ],
        )
        self.assertEqual(
            sorted([
                (event["event"], event["ticket"], event.get("reason_code"))
                for event in events
            ]),
            sorted([
                ("ticket_retired", "T-110", None),
                ("ticket_retired", "T-111", None),
                ("ticket_retired", "T-112", None),
                (
                    "canceled_ticket_retirement_waiting", "T-113",
                    "lease_invalid",
                ),
                (
                    "canceled_ticket_retirement_waiting", "T-116",
                    "lease_release_refused",
                ),
                (
                    "canceled_ticket_retirement_waiting", "T-117",
                    "publication_withdraw_refused",
                ),
                ("controller_claim_invalid", "T-113", "lease_invalid"),
            ]),
        )
        self.assertFalse(any(
            controller.claim_path(ticket).exists()
            for ticket in ("T-110", "T-111", "T-112")
        ))
        self.assertTrue(all(
            controller.claim_path(ticket).exists()
            for ticket in ("T-113", "T-114", "T-115", "T-116", "T-117")
        ))

    def test_malformed_parked_lease_is_quarantined_before_reconcile_actions(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        for ticket, lease in (("T-110", "not-a-lease"), ("T-111", "")):
            cell = self.root / f"parked/{ticket}"
            cell.mkdir(parents=True)
            controller.save_claim({
                "branch": f"ticket/{ticket}", "lease": lease,
                "parked": True, "priority": "normal", "publication_lease": "",
                "receipt": "", "role": "", "schema": CONTROL.CLAIM_SCHEMA,
                "status": "blocked", "ticket": ticket, "worktree": str(cell),
            })
        observed = []
        controller.cancellation_authority = lambda _claims: None
        controller.product_ticket_done = lambda ticket: ticket == "T-110"
        controller.operator_transition = lambda claim: observed.append(
            ("transition", claim["ticket"])
        )
        controller.recover_operator_action_events = lambda claims: observed.extend(
            ("operator-events", claim["ticket"]) for claim in claims
        )
        controller.ensure_lease = lambda claim, label: observed.append(
            (label, claim["ticket"])
        )
        controller.release_inactive_ticket_leases = lambda _claims: None
        controller.recover_missing_passport_claims = lambda _claims: None
        controller.record_qualification_done_targets = lambda: None
        controller.recover_terminal_requests = lambda _claims: None
        controller.recover_each = lambda *_args, **_kwargs: None
        controller.claim_new = lambda claims, *_args: claims
        controller.clear_admission_failure = lambda: None
        controller.runnable = lambda _claim: False
        controller.pin_routes = lambda _claims: []
        controller.role_active = lambda _claim: False

        result = controller.reconcile()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            observed,
            [("transition", "T-111"), ("operator-events", "T-111")],
        )
        self.assertIn("T-110", controller.invalid_transition_tickets)

    def test_publication_repair_releases_merge_lease_and_preserves_checkpoint(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = {
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "b" * 64,
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        calls = []
        controller.json_call = lambda *_args, **_kwargs: {
            "owner": "builder", "status": "repair",
        }
        controller.release_publication = lambda item: (
            calls.append("release"), item.update(publication_lease="")
        )
        controller.migrate_passport = lambda *_args: calls.append("passport")
        controller.event = lambda *_args, **_kwargs: calls.append("event")
        controller.publication_repair(
            claim, "c" * 64, {"pr_number": 24},
        )
        self.assertEqual(calls, ["release", "passport", "event"])

    def test_nonpublication_stage_withdraws_stale_queue_entry(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        (cell / "factory/route-plans").mkdir(parents=True)
        (cell / "factory/route-plans/T-110.json").write_text("{}\n")
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        calls = []
        controller.renew = lambda _claim: None
        controller.finish_pending_run = lambda _claim: True

        def json_call(*arguments, **_kwargs):
            calls.append(arguments[:2])
            if arguments[0] == "state-machine":
                return state_transition(
                    "AWAIT-OPERATOR product decision required"
                )
            if arguments[:2] == ("publication", "withdraw"):
                return {"status": "withdrawn"}
            raise AssertionError(arguments)

        controller.json_call = json_call
        controller.event = lambda name, *_args, **_kwargs: calls.append((name,))
        result = controller.reconcile_ticket(claim)

        self.assertEqual(result, {"status": "waiting", "ticket": "T-110"})
        self.assertIn(("publication", "withdraw"), calls)
        self.assertIn(("publication_withdrawn",), calls)

    def test_semantic_authorization_wait_is_provider_free_and_idempotent(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-semantic-wait"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        subprocess.run(
            ["git", "init", "-q", "-b", "ticket/T-110", str(cell)],
            check=True,
        )
        subprocess.run(["git", "-C", str(cell), "add", "."], check=True)
        subprocess.run(
            [
                "git", "-C", str(cell), "-c", "user.name=Test",
                "-c", "user.email=test@nysa.dev", "commit", "-qm", "input",
            ],
            check=True,
        )
        claim = {
            "branch": "ticket/T-110", "lease": "a" * 64,
            "priority": "normal", "publication_lease": "", "receipt": "",
            "role": "", "schema": CONTROL.CLAIM_SCHEMA, "status": "claimed",
            "ticket": "T-110", "worktree": str(cell),
        }
        stage = (
            "AWAIT-OPERATOR semantic-round authorization required; "
            "add exact line: OPERATOR AUTHORIZATION: spec-linter round 3"
        )
        controller.ensure_lease = lambda *_args: None
        controller.finish_pending_run = lambda *_args: True
        controller.refresh_dependency_tracking = lambda *_args: True
        controller.withdraw_publication = lambda *_args: None
        controller.run_role = lambda *_args: self.fail("provider role launched")
        controller.json_call = lambda *_args, **_kwargs: state_transition(
            stage, "c" * 64,
        )

        for _ in range(2):
            self.assertEqual(
                controller.reconcile_ticket(claim),
                {"status": "waiting", "ticket": "T-110"},
            )
        events = [
            CONTROL.read(path) for path in controller.events.glob("*.json")
            if CONTROL.read(path).get("event")
            == "semantic_round_authorization_wait"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["semantic_round"], 3)
        self.assertEqual(events[0]["role"], "spec-linter")
        self.assertEqual(events[0]["transition_receipt_sha256"], "c" * 64)
        self.assertEqual(
            claim["blocked_reason"],
            "semantic-round-authorization:spec-linter:3",
        )
        self.assertFalse(controller.runnable(claim))

    def test_same_release_semantic_authorization_import_is_exact_and_isolated(
        self,
    ) -> None:
        canonical = "OPERATOR AUTHORIZATION: spec-linter round 3"

        def commit(
            cell: Path, ticket: str, text: str, *, push: bool = True,
        ) -> str:
            path = cell / f"factory/tickets/{ticket}.md"
            path.write_text(text, encoding="utf-8")
            subprocess.run(["git", "-C", str(cell), "add", str(path)], check=True)
            subprocess.run([
                "git", "-C", str(cell), "-c", "user.name=Operator",
                "-c", "user.email=operator@nysa.dev", "commit", "-qm", "authorize",
            ], check=True)
            if push:
                subprocess.run(
                    ["git", "-C", str(cell), "push", "-q", "origin", "HEAD"],
                    check=True,
                )
            return subprocess.run(
                ["git", "-C", str(cell), "rev-parse", "HEAD"], text=True,
                capture_output=True, check=True,
            ).stdout.strip()

        def wire(controller: CONTROL.Controller, claim: dict, calls: list) -> None:
            controller.ensure_lease = lambda *_args: calls.append("lease")

            def json_call(*args, **_kwargs):
                if args[:2] == ("passport", "migrate"):
                    calls.append("migrate")
                    migrated = self.migrate_semantic_wait_passport(
                        controller, claim,
                    )
                    return {"passport": migrated["passport_sha256"], "status": "ok"}
                if args[:2] == ("passport", "validate"):
                    calls.append("validate")
                    passport = self.validate_semantic_passport(claim)
                    return {"passport": passport["passport_sha256"], "status": "ok"}
                raise AssertionError(args)

            controller.json_call = json_call

        def add_passport_gap(
            controller: CONTROL.Controller, claim: dict, cell: Path,
            transition: dict,
        ) -> None:
            ticket = claim["ticket"]
            ticket_path = cell / f"factory/tickets/{ticket}.md"
            commit(
                cell, ticket,
                ticket_path.read_text(encoding="utf-8") + "checkpoint\n",
            )
            self.migrate_semantic_wait_passport(controller, claim)
            role_head = commit(
                cell, ticket,
                ticket_path.read_text(encoding="utf-8") + "role output\n",
            )
            passport_path = self.state / f"passports/{ticket}.json"
            before = controller.authenticated_operator_passport(ticket)
            assert before is not None
            parent_file = hashlib.sha256(passport_path.read_bytes()).hexdigest()
            advanced = PASSPORT.authenticate({
                **{
                    key: value for key, value in before.items()
                    if key not in {
                        "authentication_sha256", "passport_sha256",
                        "parent_digest", "parent_file_sha256",
                    }
                },
                "head_sha": role_head,
                "head_tree": subprocess.run(
                    ["git", "-C", str(cell), "rev-parse", "HEAD^{tree}"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip(),
                "parent_digest": before["passport_sha256"],
                "parent_file_sha256": parent_file,
                "ticket_blob": subprocess.run(
                    [
                        "git", "-C", str(cell), "rev-parse",
                        f"HEAD:factory/tickets/{ticket}.md",
                    ],
                    text=True, capture_output=True, check=True,
                ).stdout.strip(),
            }, (self.state / "passport.key").read_bytes())
            PASSPORT.write_atomic(passport_path, advanced)
            transition.update(
                head_sha=role_head,
                passport_sha256=hashlib.sha256(
                    passport_path.read_bytes()
                ).hexdigest(),
            )
            transition["receipt_sha256"] = hashlib.sha256(
                CONTROL.canonical_document({
                    key: value for key, value in transition.items()
                    if key not in {"consumed", "receipt_sha256"}
                })
            ).hexdigest()
            CONTROL.write(self.state / f"{ticket}.json", transition)

        controller, claim, cell, passport, transition = self.semantic_wait_fixture(
            "same-release", "T-210",
        )
        with patch.object(
            controller, "remote_cell_head_status", side_effect=AssertionError,
        ):
            controller.recover_semantic_authorizations([claim])
        self.assertEqual(claim["status"], "waiting")
        self.assertEqual(list(controller.events.glob("*.json")), [])

        sibling_claim = controller.claim_path("T-999")
        sibling_passport = self.state / "passports/T-999.json"
        sibling_transition = self.state / "T-999.json"
        CONTROL.write(sibling_claim, {"schema": "sibling", "ticket": "T-999"})
        CONTROL.write(sibling_passport, {"schema": "sibling", "ticket": "T-999"})
        CONTROL.write(sibling_transition, {"schema": "sibling", "ticket": "T-999"})
        controller.event("sibling_checkpoint", "T-999")
        sibling_event = next(controller.events.glob("*.json"))
        sibling_bytes = {
            path: path.read_bytes() for path in (
                sibling_claim, sibling_passport, sibling_transition, sibling_event,
            )
        }

        ticket_path = cell / "factory/tickets/T-210.md"
        authorized = commit(
            cell, "T-210", ticket_path.read_text(encoding="utf-8") + "\n" + canonical,
        )
        calls: list[str] = []
        wire(controller, claim, calls)
        controller.recover_semantic_authorizations([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertNotIn("blocked_reason", claim)
        self.assertEqual(calls, ["lease", "migrate", "validate"])
        migrated = controller.authenticated_operator_passport("T-210")
        self.assertEqual(migrated["head_sha"], authorized)
        self.assertTrue(CONTROL.passport_head_lineage(
            migrated, passport["head_sha"],
        ))
        imported = [
            CONTROL.read(path) for path in controller.events.glob("*.json")
            if CONTROL.read(path).get("event")
            == "semantic_round_authorization_imported"
        ]
        self.assertEqual(len(imported), 1)
        self.assertEqual(
            {path: path.read_bytes() for path in sibling_bytes}, sibling_bytes,
        )

        crash, crash_claim, crash_cell, _old, crash_wait = self.semantic_wait_fixture(
            "same-release-crash", "T-211",
        )
        add_passport_gap(crash, crash_claim, crash_cell, crash_wait)
        crash_ticket = crash_cell / "factory/tickets/T-211.md"
        commit(
            crash_cell, "T-211",
            crash_ticket.read_text(encoding="utf-8") + canonical + "\n",
        )
        self.migrate_semantic_wait_passport(crash, crash_claim)
        restarted = CONTROL.Controller(self.args)
        restarted.worktrees_by_branch = crash.worktrees_by_branch
        restarted.ensure_lease = lambda *_args: self.fail("lease reacquired")

        def validate_only(*args, **_kwargs):
            self.assertEqual(args[:2], ("passport", "validate"))
            current = self.validate_semantic_passport(crash_claim)
            return {"passport": current["passport_sha256"], "status": "ok"}

        restarted.json_call = validate_only
        restarted.recover_semantic_authorizations([crash_claim])
        self.assertEqual(crash_claim["status"], "claimed")
        self.assertNotIn("blocked_reason", crash_claim)
        before = restarted.claim_path("T-211").read_bytes()
        restarted.recover_semantic_authorizations([crash_claim])
        self.assertEqual(restarted.claim_path("T-211").read_bytes(), before)

        mismatch, mismatch_claim, mismatch_cell, _passport, wait = (
            self.semantic_wait_fixture("same-release-mismatch", "T-212")
        )
        mismatch_ticket = mismatch_cell / "factory/tickets/T-212.md"
        commit(
            mismatch_cell, "T-212",
            mismatch_ticket.read_text(encoding="utf-8") + canonical + "\n",
        )
        wait["passport_sha256"] = "0" * 64
        wait["receipt_sha256"] = hashlib.sha256(CONTROL.canonical_document({
            key: value for key, value in wait.items()
            if key not in {"consumed", "receipt_sha256"}
        })).hexdigest()
        CONTROL.write(self.state / "T-212.json", wait)
        mismatch.json_call = lambda *_args, **_kwargs: self.fail("passport migrated")
        mismatch.recover_semantic_authorizations([mismatch_claim])
        self.assertEqual(mismatch_claim["status"], "waiting")

    def test_semantic_authorization_plan_apply_pushes_exact_child_once(self) -> None:
        def state_bytes() -> dict[Path, bytes]:
            return {
                path.relative_to(self.state): path.read_bytes()
                for path in self.state.rglob("*") if path.is_file()
            }

        controller, claim, cell, passport, _transition = (
            self.semantic_wait_fixture("semantic-control", "T-213")
        )
        parked = self.root / "parked/T-213"
        parked.parent.mkdir(exist_ok=True)
        cell.rename(parked)
        cell = parked
        claim["worktree"] = str(cell)
        claim.update(lease="", parked=True)
        controller.worktrees_by_branch = lambda: {
            "refs/heads/ticket/T-213": [str(cell)],
        }
        controller.save_claim(claim)
        leases = []

        def ensure_lease(current, reason):
            leases.append(reason)
            current.update(lease="2" * 64)
            controller.save_claim(current)

        controller.ensure_lease = ensure_lease
        parent = passport["head_sha"]
        remote = self.root / "semantic-control.git"
        sibling = self.root / "parked/T-999"
        sibling.mkdir(parents=True)
        CONTROL.write(controller.claim_path("T-999"), {
            "branch": "ticket/T-999", "lease": "invalid", "parked": True,
            "priority": "normal", "publication_lease": "", "receipt": "",
            "role": "", "schema": CONTROL.CLAIM_SCHEMA, "status": "blocked",
            "ticket": "T-999", "worktree": str(sibling),
        })
        before_plan = state_bytes()
        plan = controller.plan_semantic_authorization(
            "T-213", "spec-linter", 3, "operator",
        )
        self.assertEqual(leases, [])
        self.assertEqual(state_bytes(), before_plan)
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(cell), "rev-parse", "HEAD"], text=True,
                capture_output=True, check=True,
            ).stdout.strip(),
            parent,
        )
        self.assertEqual(
            subprocess.run(
                ["git", "--git-dir", str(remote), "rev-parse", "ticket/T-213"],
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
            parent,
        )
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "approval hash does not match",
        ):
            controller.apply_semantic_authorization(
                "T-213", "spec-linter", 3, "operator", "0" * 64,
            )
        cell_git = controller.cell_git

        def fail_first_push(claim, *arguments):
            if arguments and arguments[0] == "push":
                return subprocess.CompletedProcess(arguments, 1, "", "refused")
            return cell_git(claim, *arguments)

        controller.cell_git = fail_first_push
        with self.assertRaisesRegex(CONTROL.ControllerError, "push failed"):
            controller.apply_semantic_authorization(
                "T-213", "spec-linter", 3, "operator", plan["approval_hash"],
            )
        committed = cell_git(claim, "rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(committed, parent)
        controller.cell_git = cell_git
        retry_plan = controller.plan_semantic_authorization(
            "T-213", "spec-linter", 3, "operator",
        )
        self.assertNotEqual(retry_plan["approval_hash"], plan["approval_hash"])
        plan = retry_plan
        result = controller.apply_semantic_authorization(
            "T-213", "spec-linter", 3, "operator", plan["approval_hash"],
        )
        self.assertEqual(leases, ["semantic-round-authorization"])
        head = result["authorization_head"]
        self.assertEqual(result["status"], "applied")
        self.assertNotEqual(head, parent)
        self.assertEqual(head, committed)
        self.assertTrue(controller.exact_ticket_commit(
            claim, parent, head, authorization=True,
        ))
        self.assertEqual(
            subprocess.run(
                ["git", "--git-dir", str(remote), "rev-parse", "ticket/T-213"],
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
            head,
        )
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(cell), "diff-tree", "--no-commit-id",
                 "--name-only", "-r", head],
                text=True, capture_output=True, check=True,
            ).stdout.splitlines(),
            ["factory/tickets/T-213.md"],
        )
        replay = controller.apply_semantic_authorization(
            "T-213", "spec-linter", 3, "operator", plan["approval_hash"],
        )
        self.assertEqual(replay, result)
        self.assertEqual(
            controller.plan_semantic_authorization(
                "T-213", "spec-linter", 3, "operator",
            )["approval_hash"],
            plan["approval_hash"],
        )
        invalid, _claim, _cell, _passport, _transition = (
            self.semantic_wait_fixture("semantic-control-invalid", "T-214")
        )
        (self.state / "T-214.json").write_text("{", encoding="utf-8")
        before_refusal = state_bytes()
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "authority is unavailable",
        ):
            invalid.plan_semantic_authorization(
                "T-214", "spec-linter", 3, "operator",
            )
        self.assertEqual(state_bytes(), before_refusal)

        unsafe, _claim, unsafe_cell, _passport, _transition = (
            self.semantic_wait_fixture("semantic-control-unsafe", "T-215")
        )
        unsafe_plan = unsafe.plan_semantic_authorization(
            "T-215", "spec-linter", 3, "operator",
        )
        unsafe_ticket = unsafe_cell / "factory/tickets/T-215.md"
        original = unsafe_ticket.read_text(encoding="utf-8")
        unsafe_ticket.write_text(
            original + ("" if original.endswith("\n") else "\n")
            + "OPERATOR AUTHORIZATION: spec-linter round 3\n",
            encoding="utf-8",
        )
        os.link(unsafe_ticket, self.root / "semantic-control-hardlink")
        unsafe_head = subprocess.run(
            ["git", "-C", str(unsafe_cell), "rev-parse", "HEAD"], text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        unsafe_state = state_bytes()
        for action in (
            lambda: unsafe.plan_semantic_authorization(
                "T-215", "spec-linter", 3, "operator",
            ),
            lambda: unsafe.apply_semantic_authorization(
                "T-215", "spec-linter", 3, "operator",
                unsafe_plan["approval_hash"],
            ),
        ):
            with self.assertRaisesRegex(
                CONTROL.ControllerError, "ticket is unsafe",
            ):
                action()
        self.assertEqual(state_bytes(), unsafe_state)
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(unsafe_cell), "rev-parse", "HEAD"],
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
            unsafe_head,
        )

    def test_qualification_authorization_is_new_even_when_the_line_is_historical(
        self,
    ) -> None:
        historical = (
            "SPEC-LINT: FAIL — historical one\n"
            "SPEC-LINT: FAIL — historical two\n"
            "OPERATOR AUTHORIZATION: spec-linter round 3\n"
        )
        ticket = self.product / "factory/tickets/T-221.md"
        ticket.parent.mkdir(parents=True)
        ticket.write_text(historical, encoding="utf-8")
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.product)], check=True,
        )
        subprocess.run(["git", "-C", str(self.product), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(self.product), "-c", "user.name=Test",
            "-c", "user.email=test@nysa.dev", "commit", "-qm", "baseline",
        ], check=True)
        baseline = subprocess.run(
            ["git", "-C", str(self.product), "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        with patch.dict(os.environ, {
            "FACTORY_KIT_TRUST_SCOPE": "qualification-candidate",
            "FACTORY_QUALIFICATION_PRODUCT_SHA": baseline,
        }):
            controller, _claim, cell, _passport, _transition = (
                self.semantic_wait_fixture(
                    "qualification-semantic-control", "T-221",
                    historical_controls=historical,
                )
            )
            plan = controller.plan_semantic_authorization(
                "T-221", "spec-linter", 3, "operator",
            )
            result = controller.apply_semantic_authorization(
                "T-221", "spec-linter", 3, "operator", plan["approval_hash"],
            )
            self.assertEqual(result["status"], "applied")
            text = (cell / "factory/tickets/T-221.md").read_text(encoding="utf-8")
            self.assertEqual(
                text.splitlines().count(
                    "OPERATOR AUTHORIZATION: spec-linter round 3"
                ),
                2,
            )

    def test_contract_repair_plan_apply_pushes_exact_directive_child(self) -> None:
        controller, claim, cell, passport, transition = (
            self.semantic_wait_fixture("contract-repair-control", "T-216")
        )
        ticket = cell / "factory/tickets/T-216.md"
        before = ticket.read_text(encoding="utf-8").replace(
            "State: Planning", "State: Blocked-Escalated",
        ) + "ROLE-ESCALATE: CONTRACT-BLOCKED\n"
        ticket.write_text(before, encoding="utf-8")
        subprocess.run(["git", "-C", str(cell), "add", str(ticket)], check=True)
        subprocess.run([
            "git", "-C", str(cell), "-c", "user.name=State Machine",
            "-c", "user.email=state-machine@local", "commit", "-qm",
            "block contract",
        ], check=True)
        subprocess.run(["git", "-C", str(cell), "push", "-q"], check=True)
        head = controller.cell_git(claim, "rev-parse", "HEAD").stdout.strip()
        source = "a" * 40
        transition.update(
            consumed=True, factory_sha=source, role="test-author",
            stage="RUN test-author",
        )
        transition["receipt_sha256"] = hashlib.sha256(
            CONTROL.canonical_document({
                key: value for key, value in transition.items()
                if key not in {"consumed", "receipt_sha256"}
            })
        ).hexdigest()
        CONTROL.write(self.state / "T-216.json", transition)
        passport.pop("passport_sha256")
        passport.pop("authentication_sha256")
        route = passport["route_plan_sha256"]
        protected = passport["protected_base_sha"]
        parent_file = "b" * 64
        parent_digest = "c" * 64
        passport.update(
            current_stage="RUN test-author",
            current_state="Blocked-Escalated",
            factory_release_history=[
                {"contract_version": "2.0.0", "factory_sha": source},
                {"contract_version": "2.0.0", "factory_sha": self.release.name},
            ],
            head_sha=head,
            head_tree=controller.cell_git(claim, "rev-parse", "HEAD^{tree}").stdout.strip(),
            migration_history=[
                {
                    "from_factory_sha": source,
                    "from_head_sha": head,
                    "from_passport_file_sha256": parent_file,
                    "from_passport_sha256": parent_digest,
                    "from_protected_base_sha": protected,
                    "from_route_plan_sha256": route,
                    "schema": "nysa.software-factory.ticket-passport-migration/v2",
                    "to_factory_sha": self.release.name,
                    "to_head_sha": head,
                    "to_protected_base_sha": protected,
                    "to_route_plan_sha256": route,
                },
            ],
            parent_digest=parent_digest,
            parent_file_sha256=parent_file,
            ticket_blob=controller.cell_git(
                claim, "rev-parse", "HEAD:factory/tickets/T-216.md",
            ).stdout.strip(),
            transition_receipt_sha256=transition["receipt_sha256"],
        )
        passport = PASSPORT.authenticate(
            passport, (self.state / "passport.key").read_bytes(),
        )
        PASSPORT.write_atomic(self.state / "passports/T-216.json", passport)
        claim.update(
            blocked_reason="recovery-abandoned:targeted-repair",
            receipt=transition["receipt_sha256"], role="test-author",
            status="blocked", recovery_attempt={
                "count": CONTROL.RECOVERY_ATTEMPT_LIMIT,
                "factory_sha": self.release.name,
                "input_sha256": "f" * 64,
                "outcome_sha256": "1" * 64,
                "phase": "abandoned", "recovery": "targeted-repair",
                "retry_reason": "route-migration-required",
                "retry_status": "blocked",
            },
        )
        controller.save_claim(claim)
        controller.qualification = {
            "mode": "successor", "source_factory_sha": source,
        }
        controller.role_active = lambda _claim: False
        controller.remote_passport_valid = lambda _claim: True
        controller.terminal_for_receipt = lambda *_args: {
            "exit_status": "12", "kit_sha": source,
            "role": "test-author", "role_exit": "role_exit_contract_blocked",
            "ticket": "T-216",
            "transition_receipt_sha256": transition["receipt_sha256"],
        }
        lease_events = []
        original_apply = controller.apply_operator_ticket_change

        def ensure_lease(current, label):
            lease_events.append(("ensure", label))
            current["lease"] = "d" * 64
            current.pop("lease_released", None)
            controller.save_claim(current)

        def apply_change(current, *args, **kwargs):
            lease_events.append(("apply", "contract-repair"))
            self.assertEqual(current["lease"], "d" * 64)
            self.assertNotIn("lease_released", current)
            return original_apply(current, *args, **kwargs)

        def release_lease(current):
            lease_events.append(("release", "contract-repair"))
            current["lease_released"] = True
            controller.save_claim(current)

        controller.ensure_lease = ensure_lease
        controller.apply_operator_ticket_change = apply_change
        controller.release_ticket_lease = release_lease
        plan = controller.plan_contract_repair("T-216", "planner", "operator")
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(controller.cell_git(claim, "rev-parse", "HEAD").stdout.strip(), head)
        with self.assertRaisesRegex(CONTROL.ControllerError, "approval hash"):
            controller.apply_contract_repair(
                "T-216", "planner", "operator", "0" * 64,
            )
        result = controller.apply_contract_repair(
            "T-216", "planner", "operator", plan["approval_hash"],
        )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(lease_events, [
            ("ensure", "contract-repair"), ("apply", "contract-repair"),
            ("release", "contract-repair"),
        ])
        self.assertTrue(CONTROL.read(controller.claim_path("T-216"))["lease_released"])
        after = ticket.read_text(encoding="utf-8")
        self.assertEqual(
            after,
            before.rstrip("\n") + "\n\nOPERATOR RESUME: planner\n"
            + f"OPERATOR RESUME RECEIPT: {transition['receipt_sha256']}\n",
        )
        self.assertEqual(
            controller.cell_git(
                claim, "diff-tree", "--no-commit-id", "--name-only", "-r",
                result["repair_head"],
            ).stdout.splitlines(),
            ["factory/tickets/T-216.md"],
        )
        self.assertEqual(
            controller.plan_contract_repair("T-216", "planner", "operator")
            ["approval_hash"],
            plan["approval_hash"],
        )
        replay = controller.apply_contract_repair(
            "T-216", "planner", "operator", plan["approval_hash"],
        )
        self.assertEqual(replay["repair_head"], result["repair_head"])
        self.assertEqual(lease_events, [
            ("ensure", "contract-repair"), ("apply", "contract-repair"),
            ("release", "contract-repair"),
            ("ensure", "contract-repair"), ("apply", "contract-repair"),
            ("release", "contract-repair"),
        ])
        controller.terminal_for_receipt = lambda *_args: {
            "exit_status": "1", "kit_sha": source,
            "role": "test-author", "role_exit": "role_exit_contract_blocked",
            "ticket": "T-216",
            "transition_receipt_sha256": transition["receipt_sha256"],
        }
        with self.assertRaisesRegex(CONTROL.ControllerError, "authority is unavailable"):
            controller.plan_contract_repair("T-216", "planner", "operator")

    def test_qualification_history_repair_is_tree_identical_and_replayable(
        self,
    ) -> None:
        ticket = "T-217"
        source = "b" * 40
        cohort_source = "6" * 40
        activation_source = "e" * 40
        prior_resume_receipt = "9" * 64
        remote = self.root / "qualification-history.git"
        cell = self.root / "qualification-history-cell"
        gate_allow = self.root / "allow-history-gates"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.product)], check=True,
        )
        (self.product / ".gitignore").write_text("factory/runs/\n", encoding="utf-8")
        (self.product / "factory/PROJECT.env").write_text(
            'GH_REPO=nysa-company/product\n'
            'TEST_PATHS="app/tests/ packages/shared/tests/"\n',
            encoding="utf-8",
        )
        ticket_path = self.product / f"factory/tickets/{ticket}.md"
        ticket_path.parent.mkdir(parents=True)
        ticket_path.write_text(f"# {ticket}\n\nState: Building\n", encoding="utf-8")
        scripts = self.product / ".github/scripts"
        scripts.mkdir(parents=True)
        for name in (
            "test-immutability-check.sh", "builder-confinement-check.sh",
        ):
            path = scripts / name
            path.write_text(
                f"#!/bin/sh\ntest -f {shlex.quote(str(gate_allow))}\n",
                encoding="utf-8",
            )
            path.chmod(0o755)
        (self.product / "app/source.js").parent.mkdir(parents=True)
        (self.product / "app/source.js").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.product), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(self.product), "-c", "user.name=Test",
            "-c", "user.email=test@nysa.dev", "commit", "-qm", "base",
        ], check=True)
        base = subprocess.run(
            ["git", "-C", str(self.product), "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(self.product), "remote", "add", "origin", str(remote)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.product), "push", "-qu", "origin", "main"],
            check=True,
        )
        subprocess.run([
            "git", "-C", str(self.product), "worktree", "add", "-qb",
            f"ticket/{ticket}", str(cell), base,
        ], check=True)
        test_path = cell / "app/tests/t217.test.js"
        test_path.parent.mkdir(parents=True)
        test_path.write_text("test\n", encoding="utf-8")
        cell_ticket = cell / f"factory/tickets/{ticket}.md"
        cell_ticket.write_text(
            f"# {ticket}\n\nState: Building\nKit-SHA: {source}\n"
            "OPERATOR RESUME: planner\n"
            f"OPERATOR RESUME RECEIPT: {prior_resume_receipt}\n",
            encoding="utf-8",
        )
        route = cell / f"factory/route-plans/{ticket}.json"
        route.parent.mkdir(parents=True)
        route.write_text(
            CONTROL.canonical({
                "kit_sha": source,
                "schema": "ticket-model-route-plan/v1", "ticket": ticket,
            }) + "\n",
            encoding="utf-8",
        )
        ready = cell / f"factory/receipts/{ticket}/ready-1.json"
        ready.parent.mkdir(parents=True)
        ready.write_text("{}\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(cell), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(cell), "-c", "user.name=Test Author",
            "-c", "user.email=test@nysa.dev", "commit", "-qm", "mixed history",
        ], check=True)
        transition_head = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD"], text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        cell_ticket.write_text(
            cell_ticket.read_text(encoding="utf-8")
            + "Contract-Blocker: exact fixture\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(cell), "add", str(cell_ticket.relative_to(cell))],
            check=True,
        )
        subprocess.run([
            "git", "-C", str(cell), "-c", "user.name=Operator",
            "-c", "user.email=operator@nysa.dev", "commit", "-qm", "record blocker",
        ], check=True)
        cell_ticket.write_text(
            cell_ticket.read_text(encoding="utf-8").replace(
                "State: Building", "State: Blocked-Escalated\nResume-State: Building",
            ),
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(cell), "add", str(cell_ticket.relative_to(cell))],
            check=True,
        )
        subprocess.run([
            "git", "-C", str(cell), "-c", "user.name=Software Factory",
            "-c", "user.email=factory@local", "commit", "-qm", "transition state",
        ], check=True)
        old_head = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD"], text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        old_tree = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD^{tree}"], text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(cell), "push", "-qu", "origin", "HEAD"],
            check=True,
        )
        authorization_path = (
            self.product / "factory/migrations/inflight-release"
            / f"{self.release.name}.json"
        )
        authorization_path.parent.mkdir(parents=True)
        authorization = {
            "repository": "nysa-company/product",
            "schema": (
                "nysa.software-factory.inflight-release-authorization/v2"
            ),
            "source_kit_sha": cohort_source,
            "target_kit_sha": self.release.name,
            "tickets": [{
                "branch": f"ticket/{ticket}", "head": old_head,
                "source_kit_sha": activation_source,
                "state": "Blocked-Escalated", "ticket": ticket,
            }],
        }
        authorization_path.write_text(
            CONTROL.canonical(authorization) + "\n", encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(self.product), "add", str(
                authorization_path.relative_to(self.product)
            )], check=True,
        )
        subprocess.run([
            "git", "-C", str(self.product), "-c", "user.name=Operator",
            "-c", "user.email=operator@nysa.dev", "commit", "-qm",
            "authorize mixed source",
        ], check=True)
        qualification_product_sha = subprocess.run(
            ["git", "-C", str(self.product), "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(self.product), "push", "-q", "origin", "main"],
            check=True,
        )

        key = self.state / "passport.key"
        key.write_bytes(b"k" * 32)
        key.chmod(0o600)
        passport_path = self.state / f"passports/{ticket}.json"
        passport_path.parent.mkdir(mode=0o700)
        origin_digest = hashlib.sha256(str(remote).encode()).hexdigest()
        parent_file = "c" * 64
        parent_digest = "d" * 64
        passport = PASSPORT.authenticate({
            "base_history": [base], "branch": f"ticket/{ticket}",
            "charge_records": [], "completed_role_evidence": [],
            "contract_version": "2.0.0", "current_stage": "RUN test-author",
            "current_state": "Blocked-Escalated",
            "factory_release_history": [
                {"contract_version": "2.0.0", "factory_sha": source},
                {
                    "contract_version": "2.0.0",
                    "factory_sha": activation_source,
                },
                {
                    "contract_version": "2.0.0",
                    "factory_sha": self.release.name,
                },
            ],
            "factory_sha": self.release.name, "head_sha": old_head,
            "head_tree": old_tree, "migration_history": [
                {
                    "from_factory_sha": source, "from_head_sha": old_head,
                    "from_passport_file_sha256": "1" * 64,
                    "from_passport_sha256": "2" * 64,
                    "from_protected_base_sha": base,
                    "from_route_plan_sha256": hashlib.sha256(
                        route.read_bytes()
                    ).hexdigest(),
                    "schema": "nysa.software-factory.ticket-passport-migration/v2",
                    "to_factory_sha": activation_source,
                    "to_head_sha": old_head, "to_protected_base_sha": base,
                    "to_route_plan_sha256": hashlib.sha256(
                        route.read_bytes()
                    ).hexdigest(),
                },
                {
                    "from_factory_sha": activation_source,
                    "from_head_sha": old_head,
                    "from_passport_file_sha256": parent_file,
                    "from_passport_sha256": parent_digest,
                    "from_protected_base_sha": base,
                    "from_route_plan_sha256": hashlib.sha256(
                        route.read_bytes()
                    ).hexdigest(),
                    "schema": "nysa.software-factory.ticket-passport-migration/v2",
                    "to_factory_sha": self.release.name,
                    "to_head_sha": old_head,
                    "to_protected_base_sha": qualification_product_sha,
                    "to_route_plan_sha256": hashlib.sha256(
                        route.read_bytes()
                    ).hexdigest(),
                },
            ],
            "parent_digest": parent_digest,
            "parent_file_sha256": parent_file,
            "product_origin_sha256": origin_digest, "project": "relay",
            "protected_base_sha": qualification_product_sha,
            "publication_state": "none",
            "route_plan_sha256": hashlib.sha256(route.read_bytes()).hexdigest(),
            "schema": "nysa.software-factory.ticket-passport/v1",
            "ticket": ticket,
            "ticket_blob": subprocess.run(
                ["git", "-C", str(cell), "rev-parse", f"HEAD:factory/tickets/{ticket}.md"],
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
            "transition_receipt_sha256": "",
        }, key.read_bytes())
        PASSPORT.write_atomic(passport_path, passport)
        transition = {
            "branch": f"ticket/{ticket}", "consumed": True,
            "contract_version": "2.0.0", "factory_sha": source,
            "head_sha": transition_head, "loop": None,
            "passport_sha256": hashlib.sha256(passport_path.read_bytes()).hexdigest(),
            "project": "relay", "role": "test-author",
            "route_plan_sha256": passport["route_plan_sha256"],
            "schema": "nysa.software-factory.transition-receipt/v1",
            "stage": "RUN test-author", "ticket": ticket,
        }
        transition["receipt_sha256"] = hashlib.sha256(
            CONTROL.canonical_document({
                key: value for key, value in transition.items()
                if key not in {"consumed", "receipt_sha256"}
            })
        ).hexdigest()
        CONTROL.write(self.state / f"{ticket}.json", transition)
        passport.pop("passport_sha256")
        passport.pop("authentication_sha256")
        passport["transition_receipt_sha256"] = transition["receipt_sha256"]
        passport = PASSPORT.authenticate(passport, key.read_bytes())
        PASSPORT.write_atomic(passport_path, passport)
        terminal = self.product / "factory/runs/history.meta"
        terminal.parent.mkdir(parents=True, exist_ok=True)
        terminal.write_text(
            "run_id=history\nphase=completed\n"
            "accounting_state=abandoned_conservative\ntask_submitted=1\n"
            "effective_cost=1.000000\nexit_status=12\n"
            f"ticket={ticket}\nrole=test-author\n"
            "role_exit=role_exit_contract_blocked\n"
            f"role_head_before={transition_head}\nkit_sha={source}\n"
            "contract_version=2.0.0\n"
            f"transition_receipt_sha256={transition['receipt_sha256']}\n",
            encoding="utf-8",
        )
        terminal.chmod(0o600)
        claim = {
            "branch": f"ticket/{ticket}", "lease": "a" * 64,
            "priority": "normal", "publication_lease": "",
            "recovery_attempt": {
                "count": 1, "factory_sha": self.release.name,
                "input_sha256": "3" * 64, "outcome_sha256": "4" * 64,
                "phase": "settled", "recovery": "targeted-repair",
                "retry_reason": "", "retry_status": "blocked",
            },
            "receipt": transition["receipt_sha256"], "role": "test-author",
            "schema": CONTROL.CLAIM_SCHEMA, "status": "blocked",
            "ticket": ticket, "worktree": str(cell),
        }
        controller = CONTROL.Controller(self.args)
        controller.qualification = {
            "generation": 1, "mode": "successor",
            "source_factory_sha": cohort_source, "tickets": [ticket],
        }
        controller.qualification_manifest_sha256 = "f" * 64
        controller.save_claim(claim)
        controller.role_active = lambda _claim: False
        controller.remote_passport_valid = lambda _claim: True
        self.assertEqual(
            controller.operator_control_claim(ticket, "contract repair")["receipt"],
            transition["receipt_sha256"],
        )
        self.assertEqual(
            controller.transition_receipt(claim, allow_prior=True, record=False)[
                "receipt_sha256"
            ],
            transition["receipt_sha256"],
        )

        def migrate(current, publication, expected_head=""):
            value = PASSPORT.migrate(argparse.Namespace(
                contract_version="2.0.0", expected_head=expected_head,
                factory_root=self.product, factory_sha=self.release.name,
                project="relay", publication_state=publication, receipt="",
                run_id="", state_dir=self.state, ticket=ticket, workdir=cell,
            ), key.read_bytes())
            return {"passport": value["passport_sha256"], "status": "ok"}

        controller.migrate_passport = migrate
        environment = {
            "FACTORY_CERTIFIED_PRODUCT_ORIGIN": str(remote),
            "FACTORY_KIT_TRUST_SCOPE": "qualification-candidate",
            "FACTORY_QUALIFICATION_MODE": "isolated",
            "FACTORY_QUALIFICATION_PRODUCT_SHA": qualification_product_sha,
        }
        same_head_passport = controller.authenticated_operator_passport(ticket)
        broken_same_head = copy.deepcopy(same_head_passport)
        broken_same_head.pop("authentication_sha256")
        broken_same_head.pop("passport_sha256")
        broken_same_head["migration_history"][-1][
            "to_protected_base_sha"
        ] = "1" * 40
        PASSPORT.write_atomic(
            passport_path,
            PASSPORT.authenticate(broken_same_head, key.read_bytes()),
        )
        with patch.dict(os.environ, environment), self.assertRaisesRegex(
            CONTROL.ControllerError, "authority is unavailable",
        ):
            controller.plan_contract_repair(ticket, "test-author", "operator")
        PASSPORT.write_atomic(passport_path, same_head_passport)

        route_parent = old_head
        old_route_raw = route.read_bytes()
        old_route_sha = hashlib.sha256(old_route_raw).hexdigest()
        cell_ticket.write_text(
            cell_ticket.read_text(encoding="utf-8").replace(
                f"Kit-SHA: {source}", f"Kit-SHA: {self.release.name}",
            ),
            encoding="utf-8",
        )
        route.write_text(CONTROL.canonical({
            "kit_sha": self.release.name,
            "revisions": [{"body": {
                "kind": "migration",
                "legacy_plan_b64": base64.b64encode(old_route_raw).decode(),
                "legacy_plan_sha256": old_route_sha,
                "new_kit_sha": source, "old_kit_sha": source,
            }}, {"body": {
                "kind": "release-migration",
                "new_kit_sha": self.release.name, "old_kit_sha": source,
            }}],
            "schema": "ticket-model-route-journal/v2", "ticket": ticket,
        }) + "\n", encoding="utf-8")
        subprocess.run(
            [
                "git", "-C", str(cell), "add",
                str(route.relative_to(cell)), str(cell_ticket.relative_to(cell)),
            ],
            check=True,
        )
        subprocess.run([
            "git", "-C", str(cell), "-c", "user.name=Factory",
            "-c", "user.email=factory@nysa.dev", "commit", "-qm", "migrate route",
        ], check=True)
        old_head = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD"], text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        old_tree = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD^{tree}"], text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(cell), "push", "-q", "origin", "HEAD"],
            check=True,
        )
        with patch.dict(os.environ, environment):
            migrate(claim, "none", expected_head=old_head)
        migrated_before_repair = controller.authenticated_operator_passport(ticket)
        self.assertTrue(CONTROL.passport_head_lineage(
            migrated_before_repair, route_parent,
        ))
        controller.exact_route_migration_commit = (
            lambda _claim, before, after:
            (before, after) == (route_parent, old_head)
        )
        self.assertTrue(controller.exact_route_migration_commit(
            claim, route_parent, old_head,
        ))
        self.assertNotEqual(
            transition["route_plan_sha256"],
            migrated_before_repair["route_plan_sha256"],
        )
        bad_authorization = copy.deepcopy(authorization)
        bad_authorization["tickets"][0]["source_kit_sha"] = source
        authorization_path.write_text(
            CONTROL.canonical(bad_authorization) + "\n", encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(self.product), "add", str(
                authorization_path.relative_to(self.product)
            )], check=True,
        )
        subprocess.run([
            "git", "-C", str(self.product), "-c", "user.name=Operator",
            "-c", "user.email=operator@nysa.dev", "commit", "-qm",
            "authorize wrong ticket source",
        ], check=True)
        bad_product_sha = subprocess.run(
            ["git", "-C", str(self.product), "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        with patch.dict(os.environ, {
            **environment, "FACTORY_QUALIFICATION_PRODUCT_SHA": bad_product_sha,
        }):
            with self.assertRaisesRegex(
                CONTROL.ControllerError, "authority is unavailable",
            ):
                controller.plan_contract_repair(
                    ticket, "test-author", "operator",
                )
        with patch.dict(os.environ, {
            **environment, "FACTORY_QUALIFICATION_MODE": "takeover",
        }), self.assertRaisesRegex(
            CONTROL.ControllerError, "authority is unavailable",
        ):
            controller.plan_contract_repair(
                ticket, "test-author", "operator",
            )
        with patch.dict(os.environ, environment):
            broken_base = copy.deepcopy(migrated_before_repair)
            broken_base.pop("authentication_sha256")
            broken_base.pop("passport_sha256")
            broken_base["migration_history"][-1][
                "from_protected_base_sha"
            ] = "1" * 40
            PASSPORT.write_atomic(
                passport_path,
                PASSPORT.authenticate(broken_base, key.read_bytes()),
            )
            with self.assertRaisesRegex(
                CONTROL.ControllerError, "authority is unavailable",
            ):
                controller.plan_contract_repair(
                    ticket, "test-author", "operator",
                )
            PASSPORT.write_atomic(passport_path, migrated_before_repair)

            route_parent_tree = subprocess.run(
                ["git", "-C", str(cell), "rev-parse", f"{route_parent}^{{tree}}"],
                text=True, capture_output=True, check=True,
            ).stdout.strip()
            arbitrary_predecessor = subprocess.run([
                "git", "-C", str(cell), "-c", "user.name=Other",
                "-c", "user.email=other@example.invalid", "commit-tree",
                route_parent_tree, "-p", route_parent,
            ], input="arbitrary predecessor\n", text=True, capture_output=True,
                check=True).stdout.strip()
            predecessor = copy.deepcopy(migrated_before_repair)
            predecessor.pop("authentication_sha256")
            predecessor.pop("passport_sha256")
            predecessor["migration_history"][-2]["from_head_sha"] = (
                arbitrary_predecessor
            )
            predecessor["migration_history"][-2]["to_head_sha"] = (
                arbitrary_predecessor
            )
            predecessor["migration_history"][-1]["from_head_sha"] = (
                arbitrary_predecessor
            )
            PASSPORT.write_atomic(
                passport_path,
                PASSPORT.authenticate(predecessor, key.read_bytes()),
            )
            controller.exact_route_migration_commit = (
                lambda _claim, before, after:
                (before, after) == (arbitrary_predecessor, old_head)
            )
            with self.assertRaisesRegex(
                CONTROL.ControllerError, "authority is unavailable",
            ):
                controller.plan_contract_repair(
                    ticket, "test-author", "operator",
                )
            PASSPORT.write_atomic(passport_path, migrated_before_repair)
            controller.exact_route_migration_commit = (
                lambda _claim, before, after:
                (before, after) == (route_parent, old_head)
            )

            arbitrary_head = subprocess.run([
                "git", "-C", str(cell), "-c", "user.name=Other",
                "-c", "user.email=other@example.invalid", "commit-tree",
                old_tree, "-p", route_parent,
            ], input="arbitrary descendant\n", text=True, capture_output=True,
                check=True).stdout.strip()
            arbitrary = copy.deepcopy(migrated_before_repair)
            arbitrary.pop("authentication_sha256")
            arbitrary.pop("passport_sha256")
            arbitrary["head_sha"] = arbitrary_head
            arbitrary["migration_history"][-1]["to_head_sha"] = arbitrary_head
            PASSPORT.write_atomic(
                passport_path,
                PASSPORT.authenticate(arbitrary, key.read_bytes()),
            )
            with self.assertRaisesRegex(
                CONTROL.ControllerError, "authority is unavailable",
            ):
                controller.plan_contract_repair(
                    ticket, "test-author", "operator",
                )
            PASSPORT.write_atomic(passport_path, migrated_before_repair)

            with self.assertRaisesRegex(CONTROL.ControllerError, "gate failed"):
                controller.qualification_history_repair(
                    ticket, transition["receipt_sha256"],
                )
            self.assertFalse((self.state / f"history-reconstructions/{ticket}.json").exists())
            self.assertFalse((self.state / "history-reconstructions").exists())
            self.assertEqual(
                subprocess.run(
                    ["git", "--git-dir", str(remote), "rev-parse", f"ticket/{ticket}"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip(),
                old_head,
            )
            gate_allow.touch()
            controller.migrate_passport = lambda *_args, **_kwargs: (
                _ for _ in ()
            ).throw(CONTROL.ControllerError("simulated passport crash"))
            with self.assertRaisesRegex(
                CONTROL.ControllerError, "simulated passport crash",
            ):
                controller.qualification_history_repair(
                    ticket, transition["receipt_sha256"],
                )
            record = CONTROL.read(
                self.state / f"history-reconstructions/{ticket}.json"
            )
            self.assertEqual(record["protected_base_sha"], base)
            self.assertEqual(
                subprocess.run(
                    ["git", "--git-dir", str(remote), "rev-parse", f"ticket/{ticket}"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip(),
                record["new_head"],
            )
            self.assertEqual(
                controller.authenticated_operator_passport(ticket)["head_sha"],
                old_head,
            )
            controller.migrate_passport = migrate
            repaired = controller.qualification_history_repair(
                ticket, transition["receipt_sha256"],
            )
            replay = controller.qualification_history_repair(
                ticket, transition["receipt_sha256"],
            )
            continuation = controller.plan_contract_repair(
                ticket, "test-author", "operator",
            )
            _plan, _claim, continuation_after, _observed = (
                controller.contract_repair_plan(
                    ticket, "test-author", "operator",
                )
            )
        self.assertEqual(replay, repaired)
        self.assertEqual(continuation["status"], "planned")
        self.assertIn(
            f"OPERATOR RESUME RECEIPT: {prior_resume_receipt}\n",
            continuation_after,
        )
        self.assertIn(
            f"OPERATOR RESUME RECEIPT: {transition['receipt_sha256']}\n",
            continuation_after,
        )
        new_head = repaired["new_head"]
        self.assertNotEqual(new_head, old_head)
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(cell), "rev-parse", "HEAD^{tree}"],
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
            old_tree,
        )
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(cell), "rev-list", "--count", f"{base}..{new_head}"],
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
            "2",
        )
        self.assertEqual(cell_ticket.read_bytes(), subprocess.run(
            ["git", "-C", str(cell), "show", f"{old_head}:factory/tickets/{ticket}.md"],
            capture_output=True, check=True,
        ).stdout)
        migrated = controller.authenticated_operator_passport(ticket)
        self.assertEqual(migrated["head_sha"], new_head)
        self.assertEqual(
            migrated["migration_history"][-1]["rewrite_authorization_sha256"],
            repaired["record_sha256"],
        )
        with patch.dict(os.environ, {**environment, "FACTORY_QUALIFICATION_MODE": "takeover"}), self.assertRaisesRegex(
            CONTROL.ControllerError, "authority is unavailable",
        ):
            controller.qualification_history_repair(
                ticket, transition["receipt_sha256"],
            )
        third = subprocess.run(
            [
                "git", "-C", str(cell),
                "-c", "user.name=Qualification test",
                "-c", "user.email=qualification@example.invalid",
                "commit-tree", old_tree, "-p", new_head,
            ],
            input="unrelated\n", text=True, capture_output=True, check=True,
        ).stdout.strip()
        subprocess.run([
            "git", "-C", str(cell), "push", "-q", "--force-with-lease="
            f"refs/heads/ticket/{ticket}:{new_head}", str(remote),
            f"{third}:refs/heads/ticket/{ticket}",
        ], check=True)
        with patch.dict(os.environ, environment), self.assertRaisesRegex(
            CONTROL.ControllerError, "repository moved",
        ):
            controller.qualification_history_repair(
                ticket, transition["receipt_sha256"],
            )
        self.assertEqual(
            controller.authenticated_operator_passport(ticket)["head_sha"],
            new_head,
        )

    def test_semantic_authorization_continues_later_and_contract_rounds(
        self,
    ) -> None:
        cases = (
            ("later-spec", "T-218", "spec-linter", 4,
             "planner-spec-linter"),
            ("contract-builder", "T-219", "builder", 4,
             "contract-repair"),
            ("narrator-bundle", "T-220", "narrator", 3,
             "narrator-bundle"),
        )
        for name, ticket, role, semantic_round, kind in cases:
            with self.subTest(kind=kind):
                controller, claim, _cell, passport, _transition = (
                    self.semantic_wait_fixture(
                        name, ticket, role=role,
                        semantic_round=semantic_round, semantic_kind=kind,
                    )
                )
                plan = controller.plan_semantic_authorization(
                    ticket, role, semantic_round, "operator",
                )
                self.assertEqual(plan["semantic_kind"], kind)
                result = controller.apply_semantic_authorization(
                    ticket, role, semantic_round, "operator",
                    plan["approval_hash"],
                )
                head = result["authorization_head"]
                self.assertTrue(controller.exact_ticket_commit(
                    claim, passport["head_sha"], head,
                    authorization=True, authorization_role=role,
                    semantic_round=semantic_round, semantic_kind=kind,
                ))
                controller.ensure_lease = lambda *_args: None

                def json_call(*args, **_kwargs):
                    if args[:2] == ("passport", "migrate"):
                        migrated = self.migrate_semantic_wait_passport(
                            controller, claim,
                        )
                        return {
                            "passport": migrated["passport_sha256"],
                            "status": "ok",
                        }
                    if args[:2] == ("passport", "validate"):
                        current = self.validate_semantic_passport(claim)
                        return {
                            "passport": current["passport_sha256"],
                            "status": "ok",
                        }
                    raise AssertionError(args)

                controller.json_call = json_call
                controller.recover_semantic_authorizations([claim])
                self.assertEqual(claim["status"], "claimed")
                imported = [
                    CONTROL.read(path) for path in controller.events.glob("*.json")
                    if CONTROL.read(path).get("event")
                    == "semantic_round_authorization_imported"
                    and CONTROL.read(path).get("ticket") == ticket
                ]
                self.assertEqual(len(imported), 1)
                self.assertEqual(imported[0]["role"], role)
                self.assertEqual(imported[0]["semantic_round"], semantic_round)

        live_controls = (
            "SPEC-LINT: FAIL — one\n"
            "SPEC-LINT: PASS\n"
            "SPEC-LINT: FAIL — two\n"
            "OPERATOR AUTHORIZATION: spec-linter round 3\n"
            "SPEC-LINT: FAIL — three\n"
            "OPERATOR AUTHORIZATION: spec-linter round 4\n"
            "SPEC-LINT: PASS\n"
        )
        controller, _claim, _cell, _passport, _transition = (
            self.semantic_wait_fixture(
                "consumed-spec-grant", "T-247", semantic_round=5,
                spec_controls=live_controls,
            )
        )
        for wrong_round in (4, 6):
            with self.subTest(wrong_round=wrong_round), self.assertRaisesRegex(
                CONTROL.ControllerError,
                "semantic authorization authority is unavailable",
            ):
                controller.plan_semantic_authorization(
                    "T-247", "spec-linter", wrong_round, "operator",
                )
        plan = controller.plan_semantic_authorization(
            "T-247", "spec-linter", 5, "operator",
        )
        result = controller.apply_semantic_authorization(
            "T-247", "spec-linter", 5, "operator", plan["approval_hash"],
        )
        self.assertEqual(result["status"], "applied")

    def test_reviewer_void_plan_apply_and_import_are_exact_and_replayable(
        self,
    ) -> None:
        def state_bytes() -> dict[Path, bytes]:
            return {
                path.relative_to(self.state): path.read_bytes()
                for path in self.state.rglob("*") if path.is_file()
            }

        controller, claim, cell, passport, transition = self.reviewer_void_fixture(
            "reviewer-control", "T-216",
        )
        remote = self.root / "reviewer-control.git"
        before = state_bytes()
        plan = controller.plan_reviewer_void("T-216", 2, "operator")
        self.assertEqual(state_bytes(), before)
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(plan["reviewer_run"]["run_id"], "reviewer-2")
        self.assertEqual(
            plan["transition_lease_sha256"], transition["lease_sha256"],
        )
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "approval hash does not match",
        ):
            controller.apply_reviewer_void(
                "T-216", 2, "operator", "0" * 64,
            )
        result = controller.apply_reviewer_void(
            "T-216", 2, "operator", plan["approval_hash"],
        )
        head = result["void_head"]
        self.assertEqual(result["status"], "applied")
        self.assertTrue(controller.exact_reviewer_void_commit(
            claim, passport["head_sha"], head, 2,
        ))
        self.assertEqual(
            subprocess.run(
                ["git", "--git-dir", str(remote), "rev-parse", "ticket/T-216"],
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
            head,
        )
        self.assertEqual(
            controller.apply_reviewer_void(
                "T-216", 2, "operator", plan["approval_hash"],
            ),
            result,
        )
        with self.assertRaisesRegex(CONTROL.ControllerError, "evidence is invalid"):
            controller.plan_reviewer_void("T-216", 3, "operator")

        order: list[str] = []

        def migrate(_claim: dict, _mode: str) -> dict:
            order.append("passport")
            migrated = self.migrate_semantic_wait_passport(controller, claim)
            return {"passport": migrated["passport_sha256"], "status": "ok"}

        save = controller.save_claim

        def save_after_passport(item: dict) -> None:
            order.append("claim")
            save(item)

        controller.migrate_passport = migrate
        controller.save_claim = save_after_passport
        controller.remote_passport_valid = lambda _claim: True
        controller.ensure_lease = lambda *_args: self.fail("lease reacquired")
        controller.run_role = lambda *_args: self.fail("provider role launched")
        controller.recover_reviewer_voids([claim])
        self.assertEqual(order, ["passport", "claim"])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["lease"], "")
        self.assertNotIn("blocked_reason", claim)
        imported = [
            CONTROL.read(path) for path in controller.events.glob("*.json")
            if CONTROL.read(path).get("event") == "reviewer_run_void_imported"
        ]
        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0]["run_id"], "reviewer-2")

        crash, crash_claim, _cell, _passport, _transition = (
            self.reviewer_void_fixture("reviewer-control-crash", "T-217")
        )
        crash_plan = crash.plan_reviewer_void("T-217", 1, "operator")
        crash.apply_reviewer_void(
            "T-217", 1, "operator", crash_plan["approval_hash"],
        )
        self.migrate_semantic_wait_passport(crash, crash_claim)
        restarted = CONTROL.Controller(self.args)
        restarted.worktrees_by_branch = crash.worktrees_by_branch
        restarted.migrate_passport = lambda *_args: self.fail("passport remigrated")
        restarted.remote_passport_valid = lambda _claim: True
        restarted.ensure_lease = lambda *_args: self.fail("lease reacquired")
        restarted.run_role = lambda *_args: self.fail("provider role launched")
        restarted.recover_reviewer_voids([crash_claim])
        self.assertEqual(crash_claim["status"], "claimed")
        self.assertNotIn("blocked_reason", crash_claim)

        confined, _confined_claim, confined_cell, _passport, _transition = (
            self.reviewer_void_fixture("reviewer-control-confined", "T-218")
        )
        confined_state = state_bytes()
        for registered in (
            {},
            {"refs/heads/ticket/T-218": [
                str(confined_cell), str(self.root / "foreign-cell"),
            ]},
        ):
            confined.worktrees_by_branch = lambda value=registered: value
            with self.assertRaisesRegex(
                CONTROL.ControllerError, "worktree is unsafe",
            ):
                confined.plan_reviewer_void("T-218", 1, "operator")
            self.assertEqual(state_bytes(), confined_state)
        foreign = self.root / "foreign-cell"
        confined_cell.rename(foreign)
        confined_cell.symlink_to(foreign, target_is_directory=True)
        confined.worktrees_by_branch = lambda: {
            "refs/heads/ticket/T-218": [str(foreign)],
        }
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "worktree is unsafe",
        ):
            confined.plan_reviewer_void("T-218", 1, "operator")
        self.assertEqual(state_bytes(), confined_state)

    def test_qualification_reviewer_void_recovery_uses_current_epoch(self) -> None:
        historical = (
            "reviewer round 1: APPROVE\n"
            "OPERATOR NOTE: reviewer run 1 void — duplicate\n"
        )
        tickets = self.product / "factory/tickets"
        tickets.mkdir()
        for ticket in ("T-219", "T-220"):
            (tickets / f"{ticket}.md").write_text(
                f"# {ticket}\n\nState: Review\n" + historical,
                encoding="utf-8",
            )
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.product)], check=True,
        )
        subprocess.run(["git", "-C", str(self.product), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(self.product), "-c", "user.name=Test",
            "-c", "user.email=test@nysa.dev", "commit", "-qm", "baseline",
        ], check=True)
        baseline = subprocess.run(
            ["git", "-C", str(self.product), "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()

        with patch.dict(os.environ, {
            "FACTORY_KIT_TRUST_SCOPE": "qualification-candidate",
            "FACTORY_QUALIFICATION_PRODUCT_SHA": baseline,
        }):
            for ticket, migrated in (("T-219", False), ("T-220", True)):
                with self.subTest(ticket=ticket, migrated=migrated):
                    controller, claim, cell, _passport, _transition = (
                        self.reviewer_void_fixture(
                            f"qualification-reviewer-{ticket}", ticket,
                            controls=historical + "reviewer round 2: APPROVE\n",
                        )
                    )
                    plan = controller.plan_reviewer_void(ticket, 1, "operator")
                    controller.apply_reviewer_void(
                        ticket, 1, "operator", plan["approval_hash"],
                    )
                    self.assertEqual(
                        (cell / f"factory/tickets/{ticket}.md").read_text(
                            encoding="utf-8"
                        ).splitlines().count(
                            "OPERATOR NOTE: reviewer run 1 void — duplicate"
                        ),
                        2,
                    )
                    if migrated:
                        self.migrate_semantic_wait_passport(controller, claim)
                        runner = CONTROL.Controller(self.args)
                        runner.worktrees_by_branch = controller.worktrees_by_branch
                        runner.migrate_passport = lambda *_args: self.fail(
                            "passport remigrated"
                        )
                    else:
                        runner = controller

                        def migrate(_claim: dict, _mode: str) -> dict:
                            value = self.migrate_semantic_wait_passport(
                                controller, claim,
                            )
                            return {
                                "passport": value["passport_sha256"],
                                "status": "ok",
                            }

                        runner.migrate_passport = migrate
                    runner.remote_passport_valid = lambda _claim: True
                    runner.ensure_lease = lambda *_args: self.fail("lease reacquired")
                    runner.run_role = lambda *_args: self.fail("provider role launched")
                    runner.recover_reviewer_voids([claim])
                    self.assertEqual(claim["status"], "claimed")
                    self.assertNotIn("blocked_reason", claim)

    def test_semantic_authorization_invalid_heads_are_recoverable(self) -> None:
        canonical = "OPERATOR AUTHORIZATION: spec-linter round 3"

        def commit(cell: Path, ticket: str, text: str, push: bool = True) -> str:
            path = cell / f"factory/tickets/{ticket}.md"
            path.write_text(text, encoding="utf-8")
            subprocess.run(["git", "-C", str(cell), "add", str(path)], check=True)
            subprocess.run([
                "git", "-C", str(cell), "-c", "user.name=Operator",
                "-c", "user.email=operator@nysa.dev", "commit", "-qm", "edit",
            ], check=True)
            if push:
                subprocess.run(
                    ["git", "-C", str(cell), "push", "-q", "origin", "HEAD"],
                    check=True,
                )
            return subprocess.run(
                ["git", "-C", str(cell), "rev-parse", "HEAD"], text=True,
                capture_output=True, check=True,
            ).stdout.strip()

        controller, claim, cell, _passport, _transition = (
            self.semantic_wait_fixture(
                "duplicate-normalization", "T-220", duplicates=True,
            )
        )
        controller.recover_operator_action_events([claim])
        count_event = [
            CONTROL.read(path) for path in controller.events.glob("*.json")
            if CONTROL.read(path).get("event")
            == "semantic_round_authorization_invalid"
        ][0]
        self.assertEqual(count_event["reason_code"], "authorization_count_invalid")
        ticket_path = cell / "factory/tickets/T-220.md"
        normalized = "".join(
            line for line in ticket_path.read_text(encoding="utf-8").splitlines(
                keepends=True
            ) if line.rstrip("\n") != canonical
        ) + canonical
        commit(cell, "T-220", normalized)
        controller.ensure_lease = lambda *_args: None

        def json_call(*args, **_kwargs):
            if args[:2] == ("passport", "migrate"):
                migrated = self.migrate_semantic_wait_passport(controller, claim)
                return {"passport": migrated["passport_sha256"], "status": "ok"}
            if args[:2] == ("passport", "validate"):
                passport = self.validate_semantic_passport(claim)
                return {"passport": passport["passport_sha256"], "status": "ok"}
            raise AssertionError(args)

        controller.json_call = json_call
        controller.recover_semantic_authorizations([claim])
        self.assertEqual(claim["status"], "claimed")

        malformed, malformed_claim, malformed_cell, old, _transition = (
            self.semantic_wait_fixture("malformed-amend", "T-221")
        )
        malformed_path = malformed_cell / "factory/tickets/T-221.md"
        malformed_text = malformed_path.read_text(encoding="utf-8")
        bad_head = commit(
            malformed_cell, "T-221",
            malformed_text + "OPERATOR AUTHORIZATION: reviewer round 3\n",
        )
        malformed.recover_semantic_authorizations([malformed_claim])
        invalid = [
            CONTROL.read(path) for path in malformed.events.glob("*.json")
            if CONTROL.read(path).get("ticket") == "T-221"
        ]
        self.assertEqual(invalid[-1]["reason_code"], "authorization_content_invalid")
        subprocess.run(
            ["git", "-C", str(malformed_cell), "reset", "-q", "--hard", old["head_sha"]],
            check=True,
        )
        commit(
            malformed_cell, "T-221", malformed_text + canonical + "\n", False,
        )
        subprocess.run([
            "git", "-C", str(malformed_cell), "push", "-q", "--force",
            "origin", "HEAD",
        ], check=True)
        self.assertNotEqual(
            subprocess.run(
                ["git", "-C", str(malformed_cell), "rev-parse", "HEAD"],
                text=True, capture_output=True, check=True,
            ).stdout.strip(), bad_head,
        )
        malformed.ensure_lease = lambda *_args: None

        def amend_call(*args, **_kwargs):
            if args[:2] == ("passport", "migrate"):
                migrated = self.migrate_semantic_wait_passport(
                    malformed, malformed_claim,
                )
                return {"passport": migrated["passport_sha256"], "status": "ok"}
            passport = self.validate_semantic_passport(malformed_claim)
            return {"passport": passport["passport_sha256"], "status": "ok"}

        malformed.json_call = amend_call
        malformed.recover_semantic_authorizations([malformed_claim])
        self.assertEqual(malformed_claim["status"], "claimed")

        cases = {
            "case": "operator authorization: spec-linter round 3\n",
            "future": "OPERATOR AUTHORIZATION: spec-linter round 4\n",
            "multiple": (canonical + "\n") * 2,
            "wrong-role": "OPERATOR AUTHORIZATION: reviewer round 3\n",
        }
        for number, (name, suffix) in enumerate(cases.items(), 230):
            ticket = f"T-{number}"
            with self.subTest(case=name):
                current, current_claim, current_cell, _passport, _wait = (
                    self.semantic_wait_fixture(name, ticket)
                )
                path = current_cell / f"factory/tickets/{ticket}.md"
                commit(
                    current_cell, ticket,
                    path.read_text(encoding="utf-8") + suffix,
                )
                current.recover_semantic_authorizations([current_claim])
                events = [
                    CONTROL.read(event) for event in current.events.glob("*.json")
                    if CONTROL.read(event).get("ticket") == ticket
                ]
                self.assertEqual(
                    events[-1]["reason_code"], "authorization_content_invalid",
                )
                self.assertEqual(current_claim["status"], "waiting")

        local, local_claim, local_cell, _passport, _wait = (
            self.semantic_wait_fixture("local-only-auth", "T-240")
        )
        local_path = local_cell / "factory/tickets/T-240.md"
        commit(
            local_cell, "T-240",
            local_path.read_text(encoding="utf-8") + canonical + "\n", False,
        )
        local.recover_semantic_authorizations([local_claim])
        local_event = [
            CONTROL.read(path) for path in local.events.glob("*.json")
            if CONTROL.read(path).get("ticket") == "T-240"
        ][-1]
        self.assertEqual(local_event["reason_code"], "commit_not_pushed")

        dirty, dirty_claim, dirty_cell, _passport, _wait = (
            self.semantic_wait_fixture("dirty-auth", "T-241")
        )
        dirty_path = dirty_cell / "factory/tickets/T-241.md"
        dirty_path.write_text(
            dirty_path.read_text(encoding="utf-8") + canonical + "\n",
            encoding="utf-8",
        )
        dirty.recover_semantic_authorizations([dirty_claim])
        dirty_event = [
            CONTROL.read(path) for path in dirty.events.glob("*.json")
            if CONTROL.read(path).get("ticket") == "T-241"
        ][-1]
        self.assertEqual(dirty_event["reason_code"], "dirty_uncommitted")

        for ticket, status, reason in (
            ("T-242", "resume_ancestry_invalid", "remote_moved"),
            ("T-243", "remote_unavailable", None),
        ):
            topology, topology_claim, topology_cell, _passport, _wait = (
                self.semantic_wait_fixture("topology-" + ticket, ticket)
            )
            topology_path = topology_cell / f"factory/tickets/{ticket}.md"
            head = commit(
                topology_cell, ticket,
                topology_path.read_text(encoding="utf-8") + canonical + "\n",
                False,
            )
            with patch.object(
                topology, "remote_cell_head_status",
                return_value=(status, head, "f" * 40),
            ):
                topology.recover_semantic_authorizations([topology_claim])
            topology_events = [
                CONTROL.read(path) for path in topology.events.glob("*.json")
                if CONTROL.read(path).get("ticket") == ticket
            ]
            if reason is None:
                self.assertEqual(topology_events, [])
            else:
                self.assertEqual(topology_events[-1]["reason_code"], reason)

        eof, eof_claim, eof_cell, _passport, _wait = (
            self.semantic_wait_fixture("eof-grammar", "T-244")
        )
        eof_path = eof_cell / "factory/tickets/T-244.md"
        old_text = eof_path.read_text(encoding="utf-8").rstrip("\n")
        old_head = commit(eof_cell, "T-244", old_text)
        concatenated = commit(
            eof_cell, "T-244", old_text + canonical,
        )
        self.assertFalse(eof.exact_ticket_commit(
            eof_claim, old_head, concatenated, authorization=True,
        ))

        detached, detached_claim, detached_cell, _passport, _wait = (
            self.semantic_wait_fixture("detached-auth", "T-245")
        )
        subprocess.run(
            ["git", "-C", str(detached_cell), "checkout", "-q", "--detach"],
            check=True,
        )
        detached.recover_semantic_authorizations([detached_claim])
        detached_event = [
            CONTROL.read(path) for path in detached.events.glob("*.json")
            if CONTROL.read(path).get("ticket") == "T-245"
        ][-1]
        self.assertEqual(detached_event["reason_code"], "branch_invalid")

    def test_reconcile_recovers_missing_idle_ticket_lease(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        controller.finish_pending_run = lambda _claim: True
        calls = []

        def json_call(*arguments, **_kwargs):
            calls.append(arguments)
            if arguments[0] == "renew":
                raise CONTROL.ControllerError("lease is absent")
            if arguments[0] == "claim":
                return {
                    "lease_id": "b" * 64,
                    "schema_version": 1,
                    "ticket": "T-110",
                }
            if arguments[0] == "state-machine":
                self.assertEqual(arguments[4], "b" * 64)
                return state_transition(
                    "AWAIT-OPERATOR product decision required", "c" * 64
                )
            if arguments[:2] == ("publication", "withdraw"):
                return {"status": "absent"}
            raise AssertionError(arguments)

        controller.json_call = json_call
        controller.event = lambda name, *_args, **_kwargs: calls.append((name,))
        self.assertEqual(
            controller.reconcile_ticket(claim),
            {"status": "waiting", "ticket": "T-110"},
        )
        self.assertEqual(claim["lease"], "b" * 64)
        self.assertEqual(
            CONTROL.read(controller.claim_path("T-110"))["lease"],
            "b" * 64,
        )
        self.assertIn(("ticket_lease_recovered",), calls)

    def test_stale_publication_refreshes_before_acquiring_merge_lease(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = {
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "b" * 64,
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        calls = []
        class RecordingLock:
            def __enter__(self):
                calls.append("git-lock")

            def __exit__(self, *_args):
                calls.append("git-unlock")

        controller.git_lock = RecordingLock()
        controller.protected_base_current = lambda *_args: (
            calls.append("base") or False
        )
        controller.release_publication = lambda item: (
            calls.append("release"), item.update(publication_lease="")
        )
        controller.json_call = lambda *_args, **_kwargs: (
            calls.append("refresh") or {
                "action": "refresh", "head": "d" * 40,
            }
        )
        controller.migrate_passport = lambda *_args: calls.append("passport")
        controller.event = lambda *_args, **_kwargs: calls.append("event")
        self.assertFalse(
            controller.publication_ready(claim, "c" * 64, "d" * 40)
        )
        self.assertEqual(
            calls,
            [
                "git-lock", "base", "release", "refresh", "passport", "event",
                "git-unlock",
            ],
        )

    def test_publication_refresh_counts_as_merge_request_progress(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = {"ticket": "T-110", "worktree": str(self.root / "cell-1")}
        controller.publication_ready = lambda *_args: False
        controller.cell_git = lambda *_args: subprocess.CompletedProcess(
            [], 0, "e" * 40 + "\n", "",
        )
        controller.json_call = lambda *_args, **_kwargs: self.fail(
            "a refresh must not request auto-merge on the stale PR"
        )

        self.assertTrue(controller.request_protected_auto_merge(
            claim, "c" * 64, {"head": "d" * 40},
        ))
        controller.cell_git = lambda *_args: subprocess.CompletedProcess(
            [], 0, "d" * 40 + "\n", "",
        )
        self.assertFalse(controller.request_protected_auto_merge(
            claim, "c" * 64, {"head": "d" * 40},
        ))

    def test_publication_events_follow_serialized_lease_order(self) -> None:
        controller = CONTROL.Controller(self.args)
        release_event = threading.Event()
        allow_release_event = threading.Event()
        acquired = threading.Event()
        events = []
        first = {
            "priority": "normal", "publication_lease": "a" * 64,
            "ticket": "T-110",
        }
        second = {
            "priority": "normal", "publication_lease": "",
            "ticket": "T-111",
        }
        controller.refresh_stale_protected_base = lambda *_args: False

        def json_call(*arguments, **_kwargs):
            if arguments[:2] == ("publication", "release"):
                return {"status": "released"}
            if arguments[:2] == ("publication", "ready"):
                return {"status": "ready"}
            if arguments[:2] == ("publication", "acquire"):
                acquired.set()
                return {"lease": "b" * 64, "status": "acquired"}
            raise AssertionError(arguments)

        def event(name, ticket, **_details):
            if name == "publication_released":
                release_event.set()
                self.assertTrue(allow_release_event.wait(1))
            events.append((name, ticket))

        controller.json_call = json_call
        controller.save_claim = lambda _claim: None
        controller.event = event
        controller.event_once = event
        with ThreadPoolExecutor(max_workers=2) as executor:
            releasing = executor.submit(controller.release_publication, first)
            self.assertTrue(release_event.wait(1))
            acquiring = executor.submit(
                controller.publication_ready, second, "c" * 64, "d" * 40,
            )
            self.assertFalse(acquired.wait(0.2))
            allow_release_event.set()
            releasing.result(timeout=2)
            self.assertTrue(acquiring.result(timeout=2))

        self.assertEqual(events, [
            ("publication_released", "T-110"),
            ("publication_acquired", "T-111"),
        ])

    def test_publication_acquisition_event_recovers_before_claim_save(self) -> None:
        controller = CONTROL.Controller(self.args)
        lease = ["a" * 64]
        claim = {
            "priority": "normal", "publication_lease": "", "ticket": "T-110",
        }
        controller.refresh_stale_protected_base = lambda *_args: False
        controller.json_call = lambda *arguments, **_kwargs: (
            {"lease": lease[0], "status": "acquired"}
            if arguments[:2] == ("publication", "acquire")
            else {"status": "ready"}
        )
        controller.save_claim = lambda _claim: (_ for _ in ()).throw(
            RuntimeError("crash before claim save")
        )

        with self.assertRaisesRegex(RuntimeError, "crash before claim save"):
            controller.publication_ready(claim, "b" * 64, "c" * 40)
        self.assertEqual(len([
            path for path in controller.events.glob("*.json")
            if CONTROL.read(path).get("event") == "publication_acquired"
        ]), 1)

        restarted = CONTROL.Controller(self.args)
        recovered = dict(claim, publication_lease="")
        restarted.refresh_stale_protected_base = lambda *_args: False
        restarted.json_call = controller.json_call
        saved = []
        restarted.save_claim = lambda item: saved.append(dict(item))
        self.assertTrue(
            restarted.publication_ready(recovered, "b" * 64, "c" * 40)
        )
        self.assertEqual(saved[0]["publication_lease"], "a" * 64)
        self.assertEqual(len([
            path for path in restarted.events.glob("*.json")
            if CONTROL.read(path).get("event") == "publication_acquired"
        ]), 1)
        lease[0] = "d" * 64
        self.assertTrue(
            restarted.publication_ready(recovered, "b" * 64, "c" * 40)
        )
        self.assertEqual(len([
            path for path in restarted.events.glob("*.json")
            if CONTROL.read(path).get("event") == "publication_acquired"
        ]), 2)

    def test_pushed_publication_refresh_recovers_without_another_provider(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-publication-refresh-replay"
        refresh = cell / "factory/attestations/T-110/refresh.json"
        refresh.parent.mkdir(parents=True)
        refresh.write_text("{}\n", encoding="utf-8")
        passport_path = self.state / "passports/T-110.json"
        old_head = "c" * 40
        digest = self.operator_passport(
            "T-110", "Approved", "validating", head_sha=old_head,
        )
        claim = {
            "blocked_reason": "external-unavailable",
            "branch": "ticket/T-110",
            "lease": "",
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "release_refresh_required": True,
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        head = "d" * 40
        CONTROL.write(controller.reconciliation_marker("T-110"), {
            "branch": claim["branch"],
            "factory_sha": self.release.name,
            "head_sha": old_head,
            "passport_sha256": digest,
            "run_snapshot_sha256": controller.ticket_run_snapshot("T-110"),
            "schema": "nysa.software-factory.reconciliation-boundary/v1",
            "ticket": "T-110",
        })
        calls = []
        controller.role_active = lambda _claim: False
        controller.ticket_release_current = lambda _claim: True
        controller.remote_cell_head_status = lambda _claim: (
            "resume_commit_not_pushed", head, old_head,
        )
        controller.cell_git = lambda _claim, *arguments: subprocess.CompletedProcess(
            arguments, 0,
            stdout=(f"{head}\n" if arguments[0] == "log" else ""),
            stderr="",
        )
        controller.ensure_lease = lambda item, label: (
            calls.append(("ensure", label)), item.update(lease="a" * 64)
        )

        def json_call(*arguments, **_kwargs):
            calls.append(("replay", arguments))
            self.assertEqual(arguments[-2:], ("dependency-refresh-replay", "--json"))
            return {
                "action": "dependency-publication-refresh",
                "head": head,
            }

        controller.json_call = json_call
        controller.migrate_passport = lambda _claim, state: calls.append(
            ("passport", state)
        )
        controller.remote_passport_valid = lambda _claim: (
            calls.append("remote") or True
        )
        controller.event_once = (
            lambda name, _ticket, **_details: calls.append(("event", name))
        )

        controller.recover_prepublication_attestations([claim])

        self.assertEqual(claim["status"], "claimed")
        self.assertNotIn("blocked_reason", claim)
        self.assertNotIn("release_refresh_required", claim)
        self.assertFalse(controller.reconciliation_marker("T-110").exists())
        self.assertEqual(
            calls,
            [
                ("ensure", "publication-refresh-replay"),
                ("replay", (
                    "ticket-attest", "--ticket", "T-110", "--lease",
                    "a" * 64, "--workdir", str(cell), "--action",
                    "dependency-refresh-replay", "--json",
                )),
                ("passport", "validating"),
                "remote",
                ("event", "publication_refresh_recovered"),
            ],
        )

    def test_historical_publication_refresh_is_not_replayed(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-historical-publication-refresh"
        refresh = cell / "factory/attestations/T-110/refresh.json"
        refresh.parent.mkdir(parents=True)
        refresh.write_text("{}\n", encoding="utf-8")
        claim = {
            "branch": "ticket/T-110", "lease": "", "receipt": "",
            "role": "", "ticket": "T-110", "worktree": str(cell),
        }
        calls = []
        heads = iter(("e" * 40, "d" * 40))
        controller.cell_git = lambda _claim, *_args: subprocess.CompletedProcess(
            _args, 0, stdout=f"{next(heads)}\n", stderr="",
        )
        controller.ensure_lease = lambda *_args: calls.append("ensure")
        controller.json_call = lambda *_args, **_kwargs: calls.append("replay")

        self.assertFalse(controller.recover_pushed_publication_refresh(claim))
        self.assertEqual(calls, [])

    def test_stale_base_refreshes_before_reviewer_without_provider_run(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        claim = {
            "branch": "ticket/T-110", "lease": "a" * 64,
            "priority": "normal", "publication_lease": "", "receipt": "",
            "role": "", "schema": CONTROL.CLAIM_SCHEMA, "status": "claimed",
            "ticket": "T-110", "worktree": str(cell),
        }
        calls = []
        controller.renew = lambda _claim: None
        controller.finish_pending_run = lambda _claim: True
        controller.refresh_dependency_tracking = lambda _claim: True
        controller.ticket_merged = lambda _claim: False
        controller.protected_base_current = lambda *_args: (
            calls.append("base") or False
        )
        controller.withdraw_publication = lambda *_args: calls.append("withdraw")
        controller.migrate_passport = lambda *_args: calls.append("passport")
        controller.event = lambda name, *_args, **_kwargs: calls.append(name)
        controller.run_role = lambda *_args, **_kwargs: calls.append("provider")

        def json_call(*arguments, **_kwargs):
            if arguments[0] == "state-machine":
                return state_transition("RUN reviewer", "b" * 64)
            if arguments[0] == "ticket-pr":
                return {"head": "d" * 40, "pr_number": 24, "status": "prepared"}
            if arguments[0] == "ticket-attest":
                calls.append("refresh")
                return {"action": "refresh", "head": "e" * 40}
            raise AssertionError(arguments)

        controller.json_call = json_call
        self.assertEqual(
            controller.reconcile_ticket(claim),
            {"status": "progressed", "ticket": "T-110"},
        )
        self.assertNotIn("provider", calls)
        self.assertEqual(calls.count("refresh"), 1)
        self.assertIn("protected_base_refreshed_before_evidence", calls)

        calls.clear()
        controller.protected_base_current = lambda *_args: True
        controller.reconcile_ticket(claim)
        self.assertEqual(calls, ["withdraw", "provider"])

    def test_closeout_fetch_uses_shared_git_lock(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cells/cell-1"
        cell.mkdir(parents=True)
        done = cell.parent / "closeout-T-110/factory/attestations/T-110/done.json"
        done.parent.mkdir(parents=True)
        done.write_text("{}\n", encoding="utf-8")
        calls = []

        class RecordingLock:
            def __enter__(self):
                calls.append("git-lock")

            def __exit__(self, *_args):
                calls.append("git-unlock")

        controller.git_lock = RecordingLock()
        controller.json_call = lambda *_args, **_kwargs: (
            calls.append("done") or {"closeout_pr_state": "OPEN"}
        )

        def run(command, **_kwargs):
            calls.append("fetch")
            return CONTROL.subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(CONTROL.subprocess, "run", side_effect=run):
            self.assertFalse(controller.closeout({
                "lease": "a" * 64,
                "ticket": "T-110",
                "worktree": str(cell),
            }))
        self.assertEqual(calls, ["git-lock", "fetch", "git-unlock", "done"])

    def test_closeout_fast_forwards_clean_unattested_retry(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cells/cell-1"
        cell.mkdir(parents=True)
        worktree = cell.parent / "closeout-T-110"
        worktree.mkdir()
        commands = []
        controller.terminal_request = lambda *_args, **_kwargs: None
        controller.json_call = lambda *_args, **_kwargs: {
            "closeout_pr_state": "OPEN"
        }

        def run(command, **_kwargs):
            commands.append(command)
            if "merge" in command:
                self.assertTrue(_kwargs.get("capture_output"))
            output = "chore/t110-closeout\n" if "symbolic-ref" in command else ""
            return CONTROL.subprocess.CompletedProcess(command, 0, output, "")

        with patch.object(CONTROL.subprocess, "run", side_effect=run):
            self.assertFalse(controller.closeout({
                "lease": "a" * 64,
                "ticket": "T-110",
                "worktree": str(cell),
            }))
        self.assertIn(
            [
                "git", "-C", str(worktree), "merge", "--ff-only",
                "origin/main",
            ],
            commands,
        )

    def test_closeout_preserves_attested_retry(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cells/cell-1"
        cell.mkdir(parents=True)
        worktree = cell.parent / "closeout-T-110"
        done = worktree / "factory/attestations/T-110/done.json"
        done.parent.mkdir(parents=True)
        done.write_text("{}\n", encoding="utf-8")
        commands = []
        controller.terminal_request = lambda *_args, **_kwargs: None
        controller.json_call = lambda *_args, **_kwargs: {
            "closeout_pr_state": "OPEN"
        }

        def run(command, **_kwargs):
            commands.append(command)
            return CONTROL.subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(CONTROL.subprocess, "run", side_effect=run):
            self.assertFalse(controller.closeout({
                "lease": "a" * 64,
                "ticket": "T-110",
                "worktree": str(cell),
            }))
        self.assertFalse(any("merge" in command for command in commands))

    def test_closeout_dirty_retry_remains_fail_closed(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cells/cell-1"
        cell.mkdir(parents=True)
        worktree = cell.parent / "closeout-T-110"
        worktree.mkdir()
        commands = []
        controller.terminal_request = lambda *_args, **_kwargs: None

        def json_call(*_args, **_kwargs):
            raise CONTROL.ControllerError(
                "ticket-attest: done worktree must be clean"
            )

        controller.json_call = json_call

        def run(command, **_kwargs):
            commands.append(command)
            if "symbolic-ref" in command:
                output = "chore/t110-closeout\n"
            elif "status" in command:
                output = " M factory/LEDGER.csv\n"
            else:
                output = ""
            return CONTROL.subprocess.CompletedProcess(command, 0, output, "")

        with (
            patch.object(CONTROL.subprocess, "run", side_effect=run),
            self.assertRaisesRegex(
                CONTROL.ControllerError, "done worktree must be clean"
            ),
        ):
            controller.closeout({
                "lease": "a" * 64,
                "ticket": "T-110",
                "worktree": str(cell),
            })
        self.assertFalse(any("merge" in command for command in commands))

    def test_closeout_defers_while_sibling_claim_is_active(self) -> None:
        controller = CONTROL.Controller(self.args)
        active = self.product / "factory/.active-runs/T-174.reviewer.lock"
        active.parent.mkdir(parents=True)
        active.mkdir()
        events = []
        controller.event_once = lambda *args, **kwargs: events.append((args, kwargs))
        controller.json_call = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(AssertionError("closeout ran with active claim"))
        )

        self.assertFalse(controller.closeout({
            "lease": "a" * 64,
            "ticket": "T-175",
            "worktree": str(self.root / "cells/cell-1"),
        }))
        self.assertEqual(events, [(('closeout_deferred_active_claim', 'T-175'), {
            "active_claim": "T-174.reviewer.lock",
        })])

    def test_closeout_defers_behind_unmerged_sibling_closeout(self) -> None:
        controller = CONTROL.Controller(self.args)
        root = self.root / "cells"
        done = root / "closeout-T-174/factory/attestations/T-174/done.json"
        done.parent.mkdir(parents=True)
        done.write_text("{}\n", encoding="utf-8")
        events = []
        controller.product_ticket_done = lambda ticket: False
        controller.event_once = lambda *args, **kwargs: events.append((args, kwargs))
        controller.json_call = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(AssertionError("concurrent closeout ran"))
        )

        self.assertFalse(controller.closeout({
            "lease": "a" * 64,
            "ticket": "T-175",
            "worktree": str(root / "cell-1"),
        }))
        self.assertEqual(events, [(('closeout_deferred_pending_closeout', 'T-175'), {
            "pending_ticket": "T-174",
        })])

    def test_closeout_records_exact_terminal_evidence_once(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cells/cell-1"
        cell.mkdir(parents=True)
        (cell.parent / "closeout-T-110").mkdir()
        events = []
        controller.event_once = lambda *args, **kwargs: events.append((args, kwargs))
        controller.json_call = lambda *_args, **_kwargs: {
            "closeout_pr_state": "MERGED",
            "terminal": {
                "basis": "attested-done",
                "protected_main": "b" * 40,
            },
        }

        with patch.object(
            CONTROL.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ):
            self.assertTrue(controller.closeout({
                "lease": "a" * 64,
                "ticket": "T-110",
                "worktree": str(cell),
            }))

        self.assertEqual(events, [(('operator_terminal_recorded', 'T-110'), {
            "protected_main": "b" * 40,
            "terminal_basis": "attested-done",
        })])

    def test_closeout_refuses_merged_without_terminal_evidence(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cells/cell-1"
        cell.mkdir(parents=True)
        (cell.parent / "closeout-T-110").mkdir()
        controller.json_call = lambda *_args, **_kwargs: {
            "closeout_pr_state": "MERGED",
        }

        with (
            patch.object(
                CONTROL.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ),
            self.assertRaisesRegex(CONTROL.ControllerError, "protected terminal"),
        ):
            controller.closeout({
                "lease": "a" * 64,
                "ticket": "T-110",
                "worktree": str(cell),
            })

    def test_closeout_waits_for_post_merge_check_propagation(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cells/cell-1"
        cell.mkdir(parents=True)
        (cell.parent / "closeout-T-110").mkdir()
        events = []
        controller.event = lambda *args, **kwargs: events.append((args, kwargs))
        controller.json_call = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(CONTROL.ControllerError(
                "ticket-attest: required post-merge check is pending: ci"
            ))
        )
        with patch.object(
            CONTROL.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ):
            self.assertFalse(controller.closeout({
                "lease": "a" * 64,
                "ticket": "T-110",
                "worktree": str(cell),
            }))
        self.assertEqual(events, [(("post_merge_check_wait", "T-110"), {
            "reason": "required post-merge check is pending: ci",
        })])


class IncidentReporterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name) / "controller"
        self.events = self.state / "events"
        self.events.mkdir(parents=True, mode=0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_event(self, ordinal: int, *, reportable: bool = True) -> None:
        value = {
            "error": "api_key=do-not-publish",
            "event": "ticket_worker_failed",
            "factory_sha": "a" * 40,
            "failure_class": "factory_defect" if reportable else "unknown",
            "observed_at_epoch_ns": ordinal,
            "reason_code": "controller_worker_exception",
            "schema": "nysa.software-factory.controller-event/v1",
            "ticket": f"T-{ordinal}",
        }
        value["event_sha256"] = hashlib.sha256(
            REPORTER.canonical({
                key: item for key, item in value.items()
                if key != "event_sha256"
            }).encode()
        ).hexdigest()
        path = self.events / f"{ordinal}-aaaaaaaaaaaaaaaa.json"
        path.write_text(REPORTER.canonical(value) + "\n", encoding="utf-8")
        path.chmod(0o600)

    def test_reporter_creates_once_comments_on_recurrence_and_sanitizes(self) -> None:
        self.write_event(1)
        self.write_event(2, reportable=False)
        issue_body = ""
        created = 0
        commented = 0

        def run(command, **_kwargs):
            nonlocal issue_body, created, commented
            if command[1:4] == ("api", "--method", "GET"):
                items = [] if not issue_body else [{
                    "body": issue_body, "number": 42,
                }]
                return subprocess.CompletedProcess(
                    command, 0, json.dumps({"items": items}), ""
                )
            body_path = Path(command[command.index("--body-file") + 1])
            body = body_path.read_text(encoding="utf-8")
            if command[1:3] == ("issue", "create"):
                created += 1
                issue_body = body
            elif command[1:3] == ("issue", "comment"):
                commented += 1
            return subprocess.CompletedProcess(command, 0, "ok\n", "")

        with patch.object(REPORTER.subprocess, "run", side_effect=run):
            first = REPORTER.report(
                self.state, "nysa-company/software-factory", "relay"
            )
            self.write_event(3)
            second = REPORTER.report(
                self.state, "nysa-company/software-factory", "relay"
            )
            third = REPORTER.report(
                self.state, "nysa-company/software-factory", "relay"
            )

        self.assertEqual([first["published"], second["published"], third["published"]], [1, 1, 0])
        self.assertEqual((created, commented), (1, 1))
        self.assertIn("sf-incident-fingerprint", issue_body)
        self.assertNotIn("do-not-publish", issue_body)

    def test_reporter_leaves_github_failure_pending(self) -> None:
        self.write_event(1)
        failure = subprocess.CompletedProcess(("gh",), 1, "", "unavailable")
        with (
            patch.object(REPORTER.subprocess, "run", return_value=failure),
            self.assertRaisesRegex(REPORTER.ReportError, "GitHub command failed"),
        ):
            REPORTER.report(
                self.state, "nysa-company/software-factory", "relay"
            )
        self.assertFalse((self.state / "incident-reporter.json").exists())


if __name__ == "__main__":
    unittest.main()
