#!/usr/bin/env python3
"""Focused tests for measured, bounded product certification."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts/certification-runner.py"


class CertificationRunnerTest(unittest.TestCase):
    def run_plan(self, root: Path, phases: list[dict], workers: int = 2):
        plan = root / "plan.json"
        plan.write_text(json.dumps({
            "schema": "nysa.software-factory.certification-plan/v1",
            "phases": phases,
        }))
        result = root / "results" / "result.json"
        environment = os.environ.copy()
        environment.update(
            FACTORY_KIT_SHA="a" * 40,
            FACTORY_PRODUCT_TREE="b" * 40,
        )
        completed = subprocess.run(
            [
                sys.executable, str(RUNNER), "--plan", str(plan),
                "--result", str(result), "--workers", str(workers),
            ],
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            timeout=10,
        )
        return completed, json.loads(result.read_text())

    def test_two_independent_phases_run_concurrently_and_bind_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            helper = root / "peer.py"
            helper.write_text(
                "import pathlib,sys,time\n"
                "mine,other=map(pathlib.Path,sys.argv[1:])\n"
                "mine.write_text('ready')\n"
                "for _ in range(100):\n"
                "  if other.exists(): break\n"
                "  time.sleep(.02)\n"
                "else: raise SystemExit(9)\n"
            )
            phases = [
                {
                    "artifacts": ["a.ready"],
                    "command": [sys.executable, str(helper), "a.ready", "b.ready"],
                    "depends_on": [],
                    "name": "api",
                },
                {
                    "artifacts": ["b.ready"],
                    "command": [sys.executable, str(helper), "b.ready", "a.ready"],
                    "depends_on": [],
                    "name": "web",
                },
            ]
            completed, result = self.run_plan(root, phases)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["factory_sha"], "a" * 40)
            self.assertEqual(result["product_tree"], "b" * 40)
            self.assertEqual(
                {item["name"] for item in result["phases"]}, {"api", "web"}
            )
            self.assertTrue(
                all(item["artifact_sha256"] for item in result["phases"])
            )
            self.assertTrue(
                all(item["cache_hit"] is False for item in result["phases"])
            )

    def test_failed_phase_cancels_sibling_and_never_passes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            phases = [
                {
                    "artifacts": [],
                    "command": [
                        sys.executable,
                        "-c",
                        "print('exact failure detail');raise SystemExit(7)",
                    ],
                    "depends_on": [],
                    "name": "fail",
                },
                {
                    "artifacts": [],
                    "command": [
                        sys.executable, "-c", "import time;time.sleep(5)"
                    ],
                    "depends_on": [],
                    "name": "sibling",
                },
                {
                    "artifacts": [],
                    "command": [sys.executable, "-c", "raise SystemExit(0)"],
                    "depends_on": ["fail"],
                    "name": "downstream",
                },
            ]
            completed, result = self.run_plan(root, phases)
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(result["status"], "fail")
            self.assertIn("failed-phase-output:", completed.stdout)
            self.assertIn("exact failure detail", completed.stdout)
            by_name = {item["name"]: item for item in result["phases"]}
            self.assertEqual(by_name["fail"]["exit_status"], 7)
            self.assertIsNone(by_name["downstream"]["exit_status"])

    def test_invalid_or_cyclic_plan_is_rejected_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            phases = [
                {
                    "artifacts": [],
                    "command": [sys.executable, "-c", "raise SystemExit(0)"],
                    "depends_on": ["two"],
                    "name": "one",
                },
                {
                    "artifacts": [],
                    "command": [sys.executable, "-c", "raise SystemExit(0)"],
                    "depends_on": ["one"],
                    "name": "two",
                },
            ]
            plan = root / "plan.json"
            plan.write_text(json.dumps({
                "schema": "nysa.software-factory.certification-plan/v1",
                "phases": phases,
            }))
            environment = os.environ.copy()
            environment.update(
                FACTORY_KIT_SHA="a" * 40,
                FACTORY_PRODUCT_TREE="b" * 40,
            )
            completed = subprocess.run(
                [
                    sys.executable, str(RUNNER), "--plan", str(plan),
                    "--result", str(root / "result.json"),
                ],
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("cycle", completed.stderr)
            self.assertFalse((root / "result.json").exists())


if __name__ == "__main__":
    unittest.main()
