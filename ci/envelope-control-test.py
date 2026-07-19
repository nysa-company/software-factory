#!/usr/bin/env python3
"""Focused regression tests for envelope preview, CAS, and runtime overrides."""

import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "scripts" / "envelope-control.py"


ENVIRONMENT = """\
PER_RUN_BUDGET_USD=1.00
PER_TICKET_BUDGET_USD=10.00
PER_RUN_MAX_TURNS=5
PER_RUN_TIMEOUT_MIN=10
DAILY_CAP_USD=20.00
"""

MARKDOWN = """\
# Operating envelope — Test

## Budgets

| Limit | Value | Enforced by |
|---|---|---|
| Per-run budget (USD) | $1.00 | wrapper |
| Per-ticket budget (USD) | $10.00 | wrapper |
| Per-run max turns | 5 | wrapper |
| Per-run wall-clock cap | 10 min | wrapper |
| Daily factory cap (USD) | $20.00 | wrapper |

## Retries and escalation

Operator controlled.
"""


class EnvelopeControlTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve() / "product"
        self.factory = self.root / "factory"
        self.factory.mkdir(parents=True)
        (self.factory / "ENVELOPE.env").write_text(ENVIRONMENT)
        (self.factory / "ENVELOPE.md").write_text(MARKDOWN)

    def tearDown(self):
        self.temporary.cleanup()

    def command(self, *arguments, check=True):
        result = subprocess.run(
            [str(CONTROL), *arguments],
            text=True,
            capture_output=True,
            check=False,
        )
        if check and result.returncode:
            self.fail(f"{arguments}: {result.stdout}\n{result.stderr}")
        return result

    def json_command(self, *arguments):
        return json.loads(self.command(*arguments).stdout)

    def test_role_values_inherit_then_apply_as_consistent_pair(self):
        inspected = self.json_command("inspect", "--factory-root", str(self.root))
        self.assertEqual(inspected["roles"]["builder"]["PER_RUN_BUDGET_USD"], "1.00")

        changes = (
            "--set", "BUILDER_PER_RUN_BUDGET_USD=2.50",
            "--set", "BUILDER_PER_RUN_MAX_TURNS=8",
            "--set", "BUILDER_PER_RUN_TIMEOUT_MIN=12",
        )
        preview = self.json_command("plan", "--factory-root", str(self.root), *changes)
        applied = self.json_command(
            "apply", "--factory-root", str(self.root),
            "--approve-hash", preview["preview_hash"], *changes,
        )
        self.assertEqual(applied["status"], "applied")
        inspected = self.json_command("inspect", "--factory-root", str(self.root))
        builder = inspected["roles"]["builder"]
        self.assertEqual(
            (builder["PER_RUN_BUDGET_USD"], builder["PER_RUN_MAX_TURNS"],
             builder["PER_RUN_TIMEOUT_MIN"]),
            ("2.50", "8", "12"),
        )
        self.assertIn("| builder | $2.50 | 8 | 12 min |",
                      (self.factory / "ENVELOPE.md").read_text())

    def test_stale_preview_and_symlink_are_rejected(self):
        changes = ("--set", "PER_RUN_MAX_TURNS=6")
        preview = self.json_command("plan", "--factory-root", str(self.root), *changes)
        with (self.factory / "ENVELOPE.env").open("a") as handle:
            handle.write("# concurrent operator edit\n")
        stale = self.command(
            "apply", "--factory-root", str(self.root),
            "--approve-hash", preview["preview_hash"], *changes, check=False,
        )
        self.assertEqual(stale.returncode, 2)
        self.assertIn("approval hash does not match", stale.stdout)

        markdown = self.factory / "ENVELOPE.md"
        clean_markdown = markdown.read_text()
        markdown.write_text(clean_markdown.replace("$1.00", "$9.00", 1))
        mismatch = self.command("inspect", "--factory-root", str(self.root), check=False)
        self.assertEqual(mismatch.returncode, 2)
        self.assertIn("ENVELOPE.md and ENVELOPE.env disagree", mismatch.stdout)
        markdown.write_text(clean_markdown)

        real = self.factory / "ENVELOPE.env.real"
        (self.factory / "ENVELOPE.env").replace(real)
        (self.factory / "ENVELOPE.env").symlink_to(real.name)
        unsafe = self.command("inspect", "--factory-root", str(self.root), check=False)
        self.assertEqual(unsafe.returncode, 2)
        self.assertIn("regular single-link", unsafe.stdout)

    def test_next_attempt_override_is_immutable_and_consumed_once(self):
        day = dt.datetime.now(dt.timezone.utc).date().isoformat()
        options = (
            "--scope", "next-attempt",
            "--ticket", "T-901",
            "--role", "builder",
            "--set", "BUILDER_PER_RUN_BUDGET_USD=3.00",
        )
        preview = self.json_command(
            "override-plan", "--factory-root", str(self.root), *options,
        )
        applied = self.json_command(
            "override-apply", "--factory-root", str(self.root),
            "--approve-hash", preview["preview_hash"], *options,
        )
        record = self.factory / "envelope-overrides" / f"{applied['record_id']}.json"
        before = record.read_bytes()
        effective = self.json_command(
            "effective", "--factory-root", str(self.root),
            "--ticket", "T-901", "--role", "builder", "--day", day,
        )["effective"]
        self.assertEqual(effective["PER_RUN_BUDGET_USD"], "3.00")
        self.assertEqual(effective["FACTORY_ENVELOPE_NEXT_OVERRIDE_IDS"],
                         applied["record_id"])

        self.json_command(
            "consume", "--factory-root", str(self.root),
            "--record-ids", applied["record_id"], "--run-id", "run-901",
        )
        self.assertEqual(record.read_bytes(), before)
        effective = self.json_command(
            "effective", "--factory-root", str(self.root),
            "--ticket", "T-901", "--role", "builder", "--day", day,
        )["effective"]
        self.assertEqual(effective["PER_RUN_BUDGET_USD"], "1.00")
        self.assertEqual(effective["FACTORY_ENVELOPE_NEXT_OVERRIDE_IDS"], "")

    def test_global_day_record_is_shared_beside_machine_config(self):
        day = dt.datetime.now(dt.timezone.utc).date().isoformat()
        global_env = self.root.parent / "machine" / "global.env"
        global_env.parent.mkdir()
        global_env.write_text("GLOBAL_DAILY_CAP_USD=50.00\n")
        options = (
            "--scope", "global-day", "--day", day,
            "--global-env", str(global_env),
            "--set", "GLOBAL_DAILY_CAP_USD=25.00",
        )
        preview = self.json_command(
            "override-plan", "--factory-root", str(self.root), *options,
        )
        self.json_command(
            "override-apply", "--factory-root", str(self.root),
            "--approve-hash", preview["preview_hash"], *options,
        )
        self.assertFalse((self.factory / "envelope-overrides").exists())
        effective = self.json_command(
            "effective", "--factory-root", str(self.root),
            "--ticket", "T-901", "--role", "builder", "--day", day,
            "--global-env", str(global_env),
        )["effective"]
        self.assertEqual(effective["GLOBAL_DAILY_CAP_USD"], "25.00")


if __name__ == "__main__":
    unittest.main()
