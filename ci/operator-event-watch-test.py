#!/usr/bin/env python3
"""Focused authenticated operator event watch regressions."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "operator-event-watch.py"
SPEC = importlib.util.spec_from_file_location("operator_event_watch", SCRIPT)
WATCH = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(WATCH)


class OperatorEventWatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.state = self.root / "controller"
        self.state.mkdir(mode=0o700)
        self.events = self.state / "events"
        self.events.mkdir(mode=0o700)
        self.sequence = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def source(self, event: str, ticket: str = "T-110", **details):
        self.sequence += 1
        value = {
            "event": event,
            "factory_sha": "a" * 40,
            "observed_at_epoch_ns": 1_000_000 + self.sequence,
            "schema": WATCH.EVENT_SCHEMA,
            "ticket": ticket,
            **details,
        }
        value["event_sha256"] = hashlib.sha256(
            WATCH.canonical(value).encode()
        ).hexdigest()
        return value

    def write(self, value, token: str | None = None) -> Path:
        token = token or f"{self.sequence:016x}"
        path = self.events / f"{value['observed_at_epoch_ns']}-{token}.json"
        temporary = self.state / f".event-{token}.tmp"
        temporary.write_text(WATCH.canonical(value) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, path)
        return path

    def run_watch(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(SCRIPT), "--state-dir", str(self.state),
                "--project", "relay", *arguments, "--json",
            ],
            text=True, capture_output=True, timeout=5, check=False,
        )

    def test_exact_once_cursor_restart_and_unrelated_events(self) -> None:
        self.write(self.source("ticket_released"))
        self.write(self.source(
            "awaiting_approval", passport_sha256="b" * 64,
            question="Approve this ticket to merge in Linear.",
        ))
        self.write(self.source("budget_wait", passport_sha256="c" * 64))
        first = self.run_watch("--limit", "2", "--idle-timeout-seconds", "1")
        self.assertEqual(first.returncode, 0, first.stderr)
        events = [json.loads(line) for line in first.stdout.splitlines()]
        self.assertEqual(
            [event["action"] for event in events],
            ["awaiting_approval", "budget_halt"],
        )
        terminal = self.source(
            "role_blocked", role="builder", role_exit="provider_failed",
            run_id="run-3", terminal_reason_code="provider_unavailable",
        )
        self.write(terminal)
        second = self.run_watch(
            "--cursor", events[-1]["cursor"], "--limit", "1",
            "--idle-timeout-seconds", "1",
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        recovered = json.loads(second.stdout)
        self.assertEqual(recovered["action"], "terminal_role_failure")
        repeated = self.run_watch(
            "--cursor", recovered["cursor"], "--idle-timeout-seconds", "0.2",
        )
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(repeated.stdout, "")

    def test_contract_timeout_and_blocked_escalation_are_typed(self) -> None:
        values = [
            self.source(
                "role_blocked", role="planner",
                role_exit="role_exit_contract_blocked", run_id="run-contract",
                terminal_reason_code="",
            ),
            self.source(
                "role_blocked", role="builder", role_exit="provider_failed",
                run_id="run-timeout", terminal_reason_code="soft_timeout",
            ),
            self.source(
                "state_machine_escalated", detail="contract repair required",
                passport_sha256="d" * 64,
            ),
        ]
        for value in values:
            self.write(value)
        result = self.run_watch("--limit", "3", "--idle-timeout-seconds", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [json.loads(line)["action"] for line in result.stdout.splitlines()],
            ["contract_blocker", "progress_timeout", "blocked_escalated"],
        )

    def test_sanitized_payload_is_bounded_and_contains_no_secret(self) -> None:
        detail = (
            "token=super-secret https://person:password@example.invalid/path "
            + "x" * 400
        )
        self.write(self.source("state_machine_escalated", detail=detail))
        result = self.run_watch("--limit", "1", "--idle-timeout-seconds", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        event = json.loads(result.stdout)
        encoded = json.dumps(event)
        self.assertNotIn("super-secret", encoded)
        self.assertNotIn("password@example", encoded)
        self.assertLessEqual(len(event["reason"]), 240)
        self.assertEqual(
            set(event),
            {
                "action", "cursor", "factory_sha", "observed_at_epoch_ns",
                "passport_sha256", "project", "qualification_generation",
                "qualification_manifest_sha256", "question", "reason", "role",
                "run_id", "schema", "source_event_sha256", "ticket",
            },
        )

    def test_tampered_event_and_cursor_fail_closed(self) -> None:
        path = self.write(self.source("budget_wait"))
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["ticket"] = "T-999"
        path.write_text(WATCH.canonical(raw) + "\n", encoding="utf-8")
        path.chmod(0o600)
        result = self.run_watch("--limit", "1", "--idle-timeout-seconds", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unauthenticated", result.stderr)
        malformed = self.run_watch(
            "--cursor", "not-a-valid-cursor", "--idle-timeout-seconds", "0.1"
        )
        self.assertNotEqual(malformed.returncode, 0)
        self.assertIn("cursor", malformed.stderr)

    def test_cursor_anchor_loss_is_nonzero(self) -> None:
        path = self.write(self.source("budget_wait"))
        first = self.run_watch("--limit", "1", "--idle-timeout-seconds", "1")
        cursor = json.loads(first.stdout)["cursor"]
        path.unlink()
        lost = self.run_watch(
            "--cursor", cursor, "--idle-timeout-seconds", "0.1"
        )
        self.assertNotEqual(lost.returncode, 0)
        self.assertIn("lost", lost.stderr)

    def test_live_empty_stream_directory_loss_is_nonzero(self) -> None:
        removed = False

        def remove_stream(_seconds):
            nonlocal removed
            if not removed:
                self.events.rmdir()
                removed = True

        with (
            mock.patch.object(WATCH.time, "sleep", side_effect=remove_stream),
            self.assertRaisesRegex(WATCH.WatchError, "stream was lost"),
        ):
            list(WATCH.watch(self.state, "relay", idle_timeout_seconds=1))

    def test_concurrent_atomic_append_is_observed(self) -> None:
        def delayed_write() -> None:
            time.sleep(0.2)
            self.write(self.source("budget_wait"))

        thread = threading.Thread(target=delayed_write)
        thread.start()
        result = self.run_watch("--limit", "1", "--idle-timeout-seconds", "2")
        thread.join()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["action"], "budget_halt")

    def test_unchanged_directory_is_not_rescanned(self) -> None:
        self.write(self.source("ticket_released"))
        calls = 0
        original = WATCH.os.scandir

        def counted(path):
            nonlocal calls
            calls += 1
            return original(path)

        with mock.patch.object(WATCH.os, "scandir", counted):
            self.assertEqual(list(WATCH.watch(
                self.state, "relay", idle_timeout_seconds=0.35
            )), [])
        self.assertEqual(calls, 1)

    def test_unsafe_event_mode_and_cross_project_cursor_are_refused(self) -> None:
        path = self.write(self.source("budget_wait"))
        path.chmod(0o644)
        unsafe = self.run_watch("--idle-timeout-seconds", "0.1")
        self.assertNotEqual(unsafe.returncode, 0)
        path.chmod(0o600)
        first = self.run_watch("--limit", "1", "--idle-timeout-seconds", "1")
        cursor = json.loads(first.stdout)["cursor"]
        with self.assertRaisesRegex(WATCH.WatchError, "this stream"):
            WATCH.decode_cursor(self.state, "another-project", cursor)


if __name__ == "__main__":
    unittest.main()
