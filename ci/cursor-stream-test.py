#!/usr/bin/env python3
"""Focused fail-closed coverage for Cursor's subscription stream."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STREAM = ROOT / "scripts/lib/cursor-stream.py"
ADAPTER = ROOT / "scripts/adapters/cursor-anthropic.sh"


class CursorStreamTest(unittest.TestCase):
    def run_stream(
        self, events: list[dict], repeated_error_limit: int
    ) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            metrics = root / "metrics"
            result = subprocess.run(
                [
                    str(STREAM),
                    str(metrics),
                    "claude-sonnet-5-thinking-high",
                    "claude-sonnet-5-thinking-high",
                    str(root),
                    "4",
                    str(repeated_error_limit),
                ],
                input="".join(json.dumps(event) + "\n" for event in events),
                text=True,
                capture_output=True,
                check=False,
            )
            result.metrics = metrics.read_text()  # type: ignore[attr-defined]
            return result

    def test_development_mode_refuses_second_identical_tool_error(self) -> None:
        error = {
            "type": "tool_call",
            "subtype": "completed",
            "tool_call": {
                "editToolCall": {
                    "result": {
                        "error": {
                            "error": "Expected ',' after property at position 156"
                        }
                    }
                }
            },
        }
        result = self.run_stream(
            [
                {"type": "system", "subtype": "init"},
                error,
                {"type": "retry", "subtype": "starting", "attempt": 1},
                error,
                {"type": "result", "subtype": "success"},
            ],
            2,
        )
        self.assertEqual(result.returncode, 15)
        self.assertIn("identical tool failure limit reached: 2 >= 2", result.stderr)
        self.assertIn("internal_retries=1", result.metrics)
        self.assertIn("repeated_tool_error_count=2", result.metrics)
        self.assertNotIn('"type":"result"', result.stdout)

    def test_legacy_default_can_observe_repeated_error_and_complete(self) -> None:
        error = {
            "type": "tool_call",
            "subtype": "completed",
            "tool_call": {
                "editToolCall": {
                    "result": {"error": {"error": "same deterministic error"}}
                }
            },
        }
        result = self.run_stream(
            [
                error,
                error,
                {"type": "result", "subtype": "success"},
            ],
            0,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("repeated_tool_error_count=2", result.metrics)

    def test_different_tool_errors_do_not_trip_fuse(self) -> None:
        def error(message: str) -> dict:
            return {
                "type": "tool_call",
                "subtype": "completed",
                "tool_call": {
                    "editToolCall": {"result": {"error": {"error": message}}}
                },
            }

        result = self.run_stream(
            [
                error("first"),
                error("second"),
                {"type": "result", "subtype": "success"},
            ],
            2,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("repeated_tool_error_count=1", result.metrics)

    def test_adapter_terminates_repeated_error_producer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake = root / "agent"
            pid_file = root / "agent.pid"
            fake.write_text(
                """#!/usr/bin/env bash
case "${1:-}" in
  --version) printf '2026.07.test\\n'; exit ;;
  models) printf 'claude-sonnet-5-thinking-high\\n'; exit ;;
esac
printf '%s\\n' "$$" >"$FAKE_CURSOR_PID_FILE"
printf '%s\\n' '{"type":"system","subtype":"init"}'
printf '%s\\n' '{"type":"tool_call","subtype":"completed","tool_call":{"editToolCall":{"result":{"error":{"error":"same deterministic edit error"}}}}}'
printf '%s\\n' '{"type":"retry","subtype":"starting","attempt":1}'
printf '%s\\n' '{"type":"tool_call","subtype":"completed","tool_call":{"editToolCall":{"result":{"error":{"error":"same deterministic edit error"}}}}}'
exec sleep 20
"""
            )
            fake.chmod(0o700)
            env = os.environ.copy()
            env.update(
                {
                    "CURSOR_AGENT_BIN": str(fake),
                    "CURSOR_AGENT_VERSION": "2026.07.test",
                    "FACTORY_CURSOR_SESSION_HOME": str(root),
                    "FACTORY_CURSOR_REPEATED_TOOL_ERROR_LIMIT": "2",
                    "FAKE_CURSOR_PID_FILE": str(pid_file),
                    "TMPDIR": str(root),
                }
            )
            started = time.monotonic()
            result = subprocess.run(
                [
                    str(ADAPTER),
                    "--budget",
                    "1",
                    "--max-turns",
                    "4",
                    "--timeout-min",
                    "1",
                    "--prompt-file",
                    "/dev/null",
                    "--workdir",
                    str(root),
                    "--model",
                    "claude-sonnet-5-thinking-high",
                    "--effort",
                    "medium",
                    "--",
                    "test",
                ],
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(result.returncode, 15, result.stderr)
            self.assertLess(time.monotonic() - started, 5)
            self.assertIn("repeated identical tool failure", result.stderr)
            pid = int(pid_file.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()
