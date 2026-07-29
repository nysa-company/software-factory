#!/usr/bin/env python3
"""Focused Contract 1.8 budget-stop test."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "budget_stage", ROOT / "scripts/budget-stage.py"
)
assert SPEC and SPEC.loader
BUDGET = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUDGET)


class BudgetStageTest(unittest.TestCase):
    def test_exact_exhaustion_waits_without_another_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            product = Path(temporary)
            (product / "factory/runs").mkdir(parents=True)
            (product / "factory/ENVELOPE.env").write_text(
                "PER_RUN_BUDGET_USD=2.000000\n"
                "PER_TICKET_BUDGET_USD=2.000000\n"
                "PER_RUN_MAX_TURNS=20\n"
                "PER_RUN_TIMEOUT_MIN=30\n"
                "DAILY_CAP_USD=100.000000\n",
                encoding="utf-8",
            )
            self.assertEqual(BUDGET.resolve(ROOT, product, "T-110"), "AVAILABLE")
            (product / "factory/runs/used.meta").write_text(
                "run_id=used\n"
                "ticket=T-110\n"
                "role=planner\n"
                "accounting_state=completed\n"
                "effective_cost=2.000000\n"
                "exit_status=5\n"
                "role_exit=budget\n",
                encoding="utf-8",
            )
            self.assertTrue(
                BUDGET.resolve(ROOT, product, "T-110").startswith("AWAIT_BUDGET")
            )
            now = datetime.now(timezone.utc).replace(microsecond=0)
            override = {
                "base_env_sha256": hashlib.sha256(
                    (product / "factory/ENVELOPE.env").read_bytes()
                ).hexdigest(),
                "changes": {"PER_TICKET_BUDGET_USD": "3.00"},
                "day": None,
                "expires_at": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "issued_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "operator_id": "operator",
                "reason": "budget_exhausted",
                "role": None,
                "schema": "factory-envelope-override/v1",
                "scope": "ticket",
                "ticket": "T-110",
            }
            raw = json.dumps(
                override, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ).encode()
            override_dir = product / "factory/envelope-overrides"
            override_dir.mkdir()
            (override_dir / f"{hashlib.sha256(raw).hexdigest()}.json").write_bytes(
                raw + b"\n"
            )
            self.assertEqual(BUDGET.resolve(ROOT, product, "T-110"), "AVAILABLE")


if __name__ == "__main__":
    unittest.main()
