#!/usr/bin/env python3
"""Focused owner-local Node runtime pin regressions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
KIT = ROOT / "scripts/factory-kit.sh"


class OwnerRuntimePinTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="owner-runtime-pin-test.")
        root = Path(self.temp.name).resolve()
        self.home = root / "home"
        self.product = root / "product"
        self.runtime = root / "node-22/bin"
        self.system = root / "system-bin"
        for path in (self.home, self.runtime, self.system):
            path.mkdir(parents=True)
        (self.product / "factory").mkdir(parents=True)
        (self.product / "factory/certification-plan.json").write_text(json.dumps(
            {
                "phases": [{
                    "artifacts": [], "command": ["true"], "depends_on": [],
                    "name": "fixture", "network": "denied",
                }],
                "runtime": {"node": "v22.22.0", "npm": "10.9.2"},
                "schema": "nysa.software-factory.certification-plan/v2",
            }
        ))
        self.write_tool(self.runtime / "node", "v22.22.0")
        self.write_tool(self.runtime / "npm", "10.9.2")
        self.write_tool(self.runtime / "npx", "10.9.2")
        self.write_tool(self.system / "node", "v25.5.0")
        self.write_tool(self.system / "npm", "11.8.0")
        self.write_tool(self.system / "npx", "11.8.0")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def write_tool(path: Path, output: str) -> None:
        path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n")
        path.chmod(0o755)

    def run_pin(self, runtime: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update({
            "FACTORY_KITS_ROOT": str(self.home / ".factory/kits"),
            "HOME": str(self.home),
            "PATH": f"{self.system}:{env['PATH']}",
        })
        return subprocess.run(
            ["bash", str(KIT), "runtime-pin", "--product", str(self.product),
             "--runtime-bin", str(runtime or self.runtime)],
            capture_output=True, text=True, env=env, check=False,
        )

    def test_owner_pin_precedes_newer_system_node(self) -> None:
        result = self.run_pin()
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = json.loads(result.stdout)
        self.assertEqual(evidence["node"], "v22.22.0")
        suffix = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        self.assertIn(
            f'SAFE_PATH="$HOME/.factory/bin:{suffix}"',
            (ROOT / "integrations/hermes/bin/factory-launch").read_text(),
        )
        self.assertIn(
            f'SAFE_PATH_SUFFIX = "{suffix}"',
            (ROOT / "scripts/owner-runtime-pin.py").read_text(),
        )
        safe_path = f"{self.home}/.factory/bin:{self.system}"
        for tool, expected in (
            ("node", "v22.22.0"), ("npm", "10.9.2"), ("npx", "10.9.2")
        ):
            observed = subprocess.check_output(
                [tool, "--version"], text=True,
                env={"HOME": str(self.home), "PATH": safe_path},
            ).strip()
            self.assertEqual(observed, expected)
            self.assertTrue((self.home / ".factory/bin" / tool).is_symlink())

    def test_mismatch_fails_before_replacing_existing_pins(self) -> None:
        self.assertEqual(self.run_pin().returncode, 0)
        prior = {
            tool: os.readlink(self.home / ".factory/bin" / tool)
            for tool in ("node", "npm", "npx")
        }
        wrong = self.runtime.parent.parent / "node-25/bin"
        wrong.mkdir(parents=True)
        for tool, value in (
            ("node", "v25.5.0"), ("npm", "11.8.0"), ("npx", "11.8.0")
        ):
            self.write_tool(wrong / tool, value)
        result = self.run_pin(wrong)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("runtime mismatch for node", result.stderr)
        self.assertEqual(prior, {
            tool: os.readlink(self.home / ".factory/bin" / tool)
            for tool in ("node", "npm", "npx")
        })


if __name__ == "__main__":
    unittest.main()
