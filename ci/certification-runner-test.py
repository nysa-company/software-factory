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
    def runtime(self):
        return {
            "node": subprocess.run(
                ["node", "--version"], text=True, capture_output=True, check=True
            ).stdout.strip(),
            "npm": subprocess.run(
                ["npm", "--version"], text=True, capture_output=True, check=True
            ).stdout.strip(),
        }

    def run_plan(
        self, root: Path, phases: list[dict], workers: int = 2,
        network_reviewed: bool = False,
    ):
        phases = [{**phase, "network": phase.get("network", "denied")} for phase in phases]
        plan = root / "plan.json"
        plan.write_text(json.dumps({
            "schema": "nysa.software-factory.certification-plan/v2",
            "phases": phases,
            "runtime": self.runtime(),
        }))
        result = root / "results" / "result.json"
        environment = os.environ.copy()
        environment.update(
            FACTORY_KIT_SHA="a" * 40,
            FACTORY_PRODUCT_TREE="b" * 40,
            FACTORY_CERTIFICATION_NETWORK_REVIEWED=(
                "1" if network_reviewed else "0"
            ),
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
            self.assertTrue(
                all(item["network_granted"] is False for item in result["phases"])
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
                "schema": "nysa.software-factory.certification-plan/v2",
                "phases": [{**phase, "network": "denied"} for phase in phases],
                "runtime": self.runtime(),
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

    def test_required_network_fails_before_phase_without_review(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            sentinel = root / "spawned"
            phases = [{
                "artifacts": [],
                "command": [sys.executable, "-c", f"open({str(sentinel)!r},'w').close()"],
                "depends_on": [],
                "name": "npm-ci",
                "network": "required",
            }]
            completed, result = self.run_plan(root, phases)
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(result["failure"]["reason_code"], "reviewed_network_required")
            self.assertFalse(sentinel.exists())

            completed, result = self.run_plan(
                root, phases, network_reviewed=True
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(sentinel.exists())
            self.assertTrue(result["phases"][0]["network_granted"])

    def test_missing_runtime_is_a_typed_preflight_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = root / "plan.json"
            plan.write_text(json.dumps({
                "phases": [{
                    "artifacts": [],
                    "command": [sys.executable, "-c", "raise SystemExit(99)"],
                    "depends_on": [],
                    "name": "fixture",
                    "network": "denied",
                }],
                "runtime": self.runtime(),
                "schema": "nysa.software-factory.certification-plan/v2",
            }))
            result = root / "result.json"
            environment = os.environ.copy()
            environment.update(
                FACTORY_KIT_SHA="a" * 40,
                FACTORY_PRODUCT_TREE="b" * 40,
                FACTORY_CERTIFICATION_NETWORK_REVIEWED="0",
                PATH=str(root),
            )
            completed = subprocess.run(
                [sys.executable, str(RUNNER), "--plan", str(plan),
                 "--result", str(result), "--workers", "1"],
                cwd=root, env=environment, text=True, capture_output=True,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(
                json.loads(result.read_text())["failure"]["reason_code"],
                "runtime_identity_mismatch",
            )


if __name__ == "__main__":
    unittest.main()
