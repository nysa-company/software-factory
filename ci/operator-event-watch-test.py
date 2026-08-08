#!/usr/bin/env python3
"""Focused authenticated operator event watch regressions."""

from __future__ import annotations

import base64
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

    def test_action_context_fields_are_type_checked(self) -> None:
        def complete():
            return self.source(
                "budget_wait", role="builder", run_id="run-1",
                passport_sha256="b" * 64, qualification_generation=1,
                qualification_manifest_sha256="c" * 64,
            )

        required = ("ticket", "observed_at_epoch_ns")
        for field in required:
            for value in ("missing", None, []):
                with self.subTest(field=field, value=value):
                    source = complete()
                    if value == "missing":
                        source.pop(field)
                    else:
                        source[field] = value
                    with self.assertRaisesRegex(
                        WATCH.WatchError, "operator action context is invalid"
                    ):
                        WATCH.action_event(source, self.state, "relay", "1-a.json")

        for value in ("missing", None, [], "not-a-sha"):
            with self.subTest(factory_sha=value):
                source = complete()
                if value == "missing":
                    source.pop("factory_sha")
                else:
                    source["factory_sha"] = value
                projected = WATCH.action_event(
                    source, self.state, "relay", "1-a.json"
                )
                self.assertEqual(projected["schema"], WATCH.DIAGNOSTIC_SCHEMA)
                self.assertEqual(projected["action"], "invalid_action_context")
                self.assertEqual(projected["reason"], "factory_identity_unavailable")
                self.assertIsNone(projected["factory_sha"])
                self.assertNotIn(str(value), WATCH.canonical(projected))

        optional = ("role", "run_id", "passport_sha256")
        for field in optional:
            for value in ("missing", None):
                with self.subTest(field=field, value=value):
                    source = complete()
                    if value == "missing":
                        source.pop(field)
                    else:
                        source[field] = value
                    projected = WATCH.action_event(
                        source, self.state, "relay", "1-a.json"
                    )
                    self.assertIsNone(projected[field])
            with self.subTest(field=field, value=[]):
                source = complete()
                source[field] = []
                with self.assertRaisesRegex(
                    WATCH.WatchError, "operator action context is invalid"
                ):
                    WATCH.action_event(source, self.state, "relay", "1-a.json")

        for generation, manifest in (
            (None, None), ("missing", "missing"),
        ):
            source = complete()
            if generation == "missing":
                source.pop("qualification_generation")
                source.pop("qualification_manifest_sha256")
            else:
                source["qualification_generation"] = generation
                source["qualification_manifest_sha256"] = manifest
            self.assertIsNotNone(
                WATCH.action_event(source, self.state, "relay", "1-a.json")
            )
        for generation, manifest in (
            (1, None), (None, "c" * 64), ("1", "c" * 64), (1, []),
        ):
            with self.subTest(generation=generation, manifest=manifest):
                source = complete()
                source["qualification_generation"] = generation
                source["qualification_manifest_sha256"] = manifest
                with self.assertRaisesRegex(
                    WATCH.WatchError,
                    "operator action qualification context is invalid",
                ):
                    WATCH.action_event(source, self.state, "relay", "1-a.json")

        for field in ("terminal_reason_code", "role_exit"):
            source = complete()
            source["event"] = "role_blocked"
            source[field] = []
            projected = WATCH.action_event(
                source, self.state, "relay", "1-a.json"
            )
            self.assertEqual(projected["action"], "terminal_role_failure")
            self.assertEqual(projected["reason"], "")

    def test_null_factory_sha_diagnostic_restart_and_idle_completion(self) -> None:
        self.write(self.source("ticket_released", factory_sha=None))
        self.write(self.source("budget_wait", factory_sha=None))
        self.write(self.source("budget_wait", ticket="T-111"))

        first = self.run_watch("--limit", "1", "--idle-timeout-seconds", "1")
        self.assertEqual(first.returncode, 0, first.stderr)
        diagnostic = json.loads(first.stdout)
        self.assertEqual(diagnostic["schema"], WATCH.DIAGNOSTIC_SCHEMA)
        self.assertEqual(diagnostic["action"], "invalid_action_context")
        self.assertEqual(diagnostic["reason"], "factory_identity_unavailable")
        self.assertIsNone(diagnostic["factory_sha"])

        second = self.run_watch(
            "--cursor", diagnostic["cursor"], "--limit", "1",
            "--idle-timeout-seconds", "1",
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        action = json.loads(second.stdout)
        self.assertEqual(action["schema"], WATCH.WATCH_SCHEMA)
        self.assertEqual(action["ticket"], "T-111")

        repeated = self.run_watch(
            "--cursor", action["cursor"], "--idle-timeout-seconds", "0.2",
        )
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(repeated.stdout, "")

    def test_variable_width_epoch_names_use_numeric_order(self) -> None:
        later = self.source("budget_wait", ticket="T-110")
        later["observed_at_epoch_ns"] = 10
        later.pop("event_sha256")
        later["event_sha256"] = hashlib.sha256(
            WATCH.canonical(later).encode()
        ).hexdigest()
        earlier = self.source("budget_wait", ticket="T-109")
        earlier["observed_at_epoch_ns"] = 9
        earlier.pop("event_sha256")
        earlier["event_sha256"] = hashlib.sha256(
            WATCH.canonical(earlier).encode()
        ).hexdigest()
        self.write(later, "0000000000000010")
        self.write(earlier, "0000000000000009")
        result = self.run_watch("--limit", "2", "--idle-timeout-seconds", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [json.loads(line)["ticket"] for line in result.stdout.splitlines()],
            ["T-109", "T-110"],
        )

    def test_filename_epoch_must_match_authenticated_observation(self) -> None:
        value = self.source("budget_wait")
        path = self.write(value)
        mismatched = self.events / f"9-{path.name.partition('-')[2]}"
        path.rename(mismatched)
        result = self.run_watch("--limit", "1", "--idle-timeout-seconds", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unauthenticated", result.stderr)

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

    def test_pre_go_and_generic_claim_blocks_are_typed_without_duplicates(
        self,
    ) -> None:
        self.write(self.source(
            "pre_go_failure_blocked", failed_run_id="pre-go-1",
            reason="cursor_credential_unsafe",
        ))
        self.write(self.source(
            "ticket_blocked", ticket="T-111",
            reason="state-machine-escalation",
        ))
        self.write(self.source(
            "state_machine_escalated", ticket="T-111",
            detail="evidence bundle remained invalid",
            passport_sha256="d" * 64,
        ))
        self.write(self.source(
            "ticket_blocked", ticket="T-112", reason="preflight",
        ))
        result = self.run_watch("--limit", "3", "--idle-timeout-seconds", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(
            [(event["action"], event["ticket"], event["run_id"]) for event in events],
            [
                ("terminal_role_failure", "T-110", "pre-go-1"),
                ("blocked_escalated", "T-111", None),
                ("blocked_escalated", "T-112", None),
            ],
        )
        restarted = self.run_watch(
            "--cursor", events[-1]["cursor"], "--idle-timeout-seconds", "0.2",
        )
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertEqual(restarted.stdout, "")

    def test_sanitized_payload_is_bounded_and_contains_no_secret(self) -> None:
        detail = (
            "password: hunter two\n"
            '{"token": "quoted json sentinel"}\n'
            "yaml_secret: |\n  continuation sentinel\n"
            "Authorization: Bearer authorization-sentinel\n"
            "https://url-user:url-sentinel@example.invalid/path\n"
            + "x" * 400
        )
        self.write(self.source("state_machine_escalated", detail=detail))
        result = self.run_watch("--limit", "1", "--idle-timeout-seconds", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        event = json.loads(result.stdout)
        encoded = json.dumps(event)
        for sentinel in (
            "hunter", "two", "quoted json sentinel", "continuation sentinel",
            "authorization-sentinel", "url-sentinel",
        ):
            self.assertNotIn(sentinel, encoded)
            self.assertNotIn(sentinel, result.stderr)
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
        path = self.write(self.source("budget_wait", factory_sha=None))
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["ticket"] = "T-999"
        path.write_text(WATCH.canonical(raw) + "\n", encoding="utf-8")
        path.chmod(0o600)
        result = self.run_watch("--limit", "1", "--idle-timeout-seconds", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unauthenticated", result.stderr)
        path.unlink()
        noncanonical = self.source("budget_wait", factory_sha=None)
        path = self.write(noncanonical)
        path.write_text(json.dumps(noncanonical) + "\n", encoding="utf-8")
        path.chmod(0o600)
        result = self.run_watch("--limit", "1", "--idle-timeout-seconds", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unauthenticated", result.stderr)
        malformed = self.run_watch(
            "--cursor", "not-a-valid-cursor", "--idle-timeout-seconds", "0.1"
        )
        self.assertNotEqual(malformed.returncode, 0)
        self.assertIn("cursor", malformed.stderr)

    def test_null_digest_and_cursor_fields_exit_typed(self) -> None:
        source = self.source("budget_wait", factory_sha=None)
        source["event_sha256"] = None
        path = self.write(source)
        result = self.run_watch("--limit", "1", "--idle-timeout-seconds", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("unauthenticated", result.stderr)
        self.assertNotIn("TypeError", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        path.unlink()

        for field in ("event", "event_sha256"):
            with self.subTest(field=field):
                cursor = {
                    "event": "1-0000000000000001.json",
                    "event_sha256": "a" * 64,
                    "project": "relay",
                    "schema": WATCH.CURSOR_SCHEMA,
                    "stream_sha256": WATCH.stream_id(self.state, "relay"),
                }
                cursor[field] = None
                token = base64.urlsafe_b64encode(
                    WATCH.canonical(cursor).encode()
                ).decode().rstrip("=")
                result = self.run_watch(
                    "--cursor", token, "--idle-timeout-seconds", "0.1"
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("cursor", result.stderr)
                self.assertNotIn("TypeError", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

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
        path = self.write(self.source("budget_wait", factory_sha=None))
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
