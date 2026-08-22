#!/usr/bin/env python3
"""Focused exact certification-runtime tuple regressions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / "scripts/certification-preflight.py"


def command(root: Path, *arguments: str) -> str:
    return subprocess.run(
        arguments, cwd=root, text=True, capture_output=True, check=True,
    ).stdout.strip()


class CertificationPreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.product = Path(self.temporary.name) / "product"
        (self.product / "factory").mkdir(parents=True)
        command(self.product, "git", "init", "-q", "-b", "main")
        command(self.product, "git", "config", "user.name", "Test")
        command(self.product, "git", "config", "user.email", "test@example.invalid")
        self.runtime = {
            "node": command(self.product, "node", "--version"),
            "npm": command(self.product, "npm", "--version"),
        }
        self.marker = self.product / "phase-spawned"
        self.write_plan(self.runtime)
        command(self.product, "git", "add", ".")
        command(self.product, "git", "commit", "-qm", "control-only plan")
        self.product_sha = command(self.product, "git", "rev-parse", "HEAD")
        self.product_tree = command(self.product, "git", "rev-parse", "HEAD^{tree}")
        self.identity = {
            "contract_version": "1.8.0",
            "factory_sha": "a" * 40,
            "factory_tree": "b" * 40,
            "product_sha": self.product_sha,
            "product_tree": self.product_tree,
        }
        self.expected = {**self.identity, **self.runtime}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_plan(self, runtime: object) -> None:
        (self.product / "factory/certification-plan.json").write_text(
            json.dumps({
                "phases": [{
                    "artifacts": [],
                    "command": [
                        sys.executable, "-c",
                        f"open({str(self.marker)!r}, 'w').close()",
                    ],
                    "depends_on": [],
                    "name": "control-only",
                    "network": "denied",
                }, {
                    "artifacts": [], "command": ["false"],
                    "depends_on": ["control-only"], "kind": "test",
                    "name": "optional-tests", "network": "denied",
                    "optional": True,
                }],
                "runtime": runtime,
                "schema": "nysa.software-factory.certification-plan/v2",
            }) + "\n",
            encoding="utf-8",
        )

    def run_preflight(self, expected: object | None = None) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        if expected is not None:
            environment["FACTORY_CERTIFICATION_TUPLE"] = json.dumps(expected)
        return subprocess.run(
            [
                sys.executable, str(PREFLIGHT),
                "--plan", str(self.product / "factory/certification-plan.json"),
                "--factory-sha", self.identity["factory_sha"],
                "--factory-tree", self.identity["factory_tree"],
                "--product-root", str(self.product),
                "--contract-version", self.identity["contract_version"],
            ],
            cwd=self.product, env=environment, text=True, capture_output=True,
        )

    def failure(self, completed: subprocess.CompletedProcess[str]) -> dict[str, str]:
        return json.loads(completed.stderr)["failure"]

    def test_exact_tuple_and_control_only_plan_pass_without_spawning_phase(self) -> None:
        for expected in (None, self.expected):
            with self.subTest(expected=expected is not None):
                completed = self.run_preflight(expected)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                value = json.loads(completed.stdout)
                self.assertEqual(value["runtime_tuple"], self.expected)
                self.assertEqual(value["phases"], ["control-only", "optional-tests"])
                self.assertEqual(value["optional_tests"], ["optional-tests"])
                self.assertFalse(self.marker.exists())

    def test_each_tuple_mismatch_is_typed_and_never_spawns_phase(self) -> None:
        replacements = {
            "factory_sha": "c" * 40,
            "factory_tree": "d" * 40,
            "product_sha": "e" * 40,
            "product_tree": "f" * 40,
            "contract_version": "9.9.9",
            "node": "v99.0.0",
            "npm": "99.0.0",
        }
        for field, replacement in replacements.items():
            with self.subTest(field=field):
                expected = {**self.expected, field: replacement}
                completed = self.run_preflight(expected)
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(self.failure(completed)["field"], field)
                self.assertEqual(
                    self.failure(completed)["reason_code"], "runtime_tuple_mismatch",
                )
                self.assertFalse(self.marker.exists())

    def test_missing_unknown_and_invalid_tuple_fields_fail_closed(self) -> None:
        cases = []
        missing = dict(self.expected)
        missing.pop("npm")
        cases.append((missing, "runtime_tuple_missing", "npm"))
        cases.append((
            {**self.expected, "future": "value"},
            "runtime_tuple_unknown", "future",
        ))
        cases.append((
            {**self.expected, "node": "latest"},
            "runtime_tuple_invalid", "node",
        ))
        for value, reason, field in cases:
            with self.subTest(reason=reason):
                completed = self.run_preflight(value)
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(self.failure(completed)["reason_code"], reason)
                self.assertEqual(self.failure(completed)["field"], field)
                self.assertFalse(self.marker.exists())

    def test_malformed_or_missing_v2_runtime_fails_before_phase(self) -> None:
        for runtime in (None, {"node": self.runtime["node"]}, {**self.runtime, "extra": "x"}):
            with self.subTest(runtime=runtime):
                self.write_plan(runtime)
                completed = self.run_preflight()
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(
                    self.failure(completed)["reason_code"],
                    "certification_plan_invalid",
                )
                self.assertFalse(self.marker.exists())

    def test_valid_plan_mutation_after_tuple_receipt_fails_before_phase(self) -> None:
        value = json.loads(
            (self.product / "factory/certification-plan.json").read_text()
        )
        value["phases"][0]["command"] = [
            sys.executable, "-c", "raise SystemExit(97)",
        ]
        (self.product / "factory/certification-plan.json").write_text(
            json.dumps(value) + "\n", encoding="utf-8",
        )
        first = self.run_preflight(self.expected)
        replay = self.run_preflight(self.expected)
        self.assertEqual(first.returncode, 2)
        self.assertEqual(
            (replay.returncode, replay.stdout, replay.stderr),
            (first.returncode, first.stdout, first.stderr),
        )
        self.assertEqual(
            self.failure(first)["reason_code"], "product_identity_dirty",
        )
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
