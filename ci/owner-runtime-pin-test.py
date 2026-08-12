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
PIN = ROOT / "scripts/owner-runtime-pin.py"


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

    def run_transaction(
        self, action: str, *arguments: str,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update({"HOME": str(self.home), "PATH": f"{self.system}:{env['PATH']}"})
        env.update(environment or {})
        return subprocess.run(
            ["python3", str(PIN), action, *arguments], capture_output=True,
            text=True, env=env, check=False,
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
        launcher = (ROOT / "integrations/hermes/bin/factory-launch").read_text()
        self.assertIn('PROJECT_RUNTIME_BIN="$PROJECT_RUNTIME_ROOT/bin"', launcher)
        self.assertIn('owner-runtime-pin.py" check', launcher)
        self.assertIn('SAFE_PATH="$PROJECT_RUNTIME_BIN:', launcher)
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

    def test_project_plan_apply_is_exact_cas_and_replay_safe(self) -> None:
        target = self.home / ".factory/kits/projects/relay/runtime/bin"
        target.parent.mkdir(parents=True)
        planned = self.run_transaction(
            "plan", "--product", str(self.product), "--runtime-bin",
            str(self.runtime), "--target-bin", str(target),
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(planned.stdout)
        self.assertEqual(plan["action"], "install")
        self.assertRegex(plan["approval_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(plan["expected"], {
            "node": "v22.22.0", "npm": "10.9.2", "npx": "10.9.2",
        })
        plan_file = self.home / "runtime-plan.json"
        plan_file.write_text(planned.stdout)
        plan_file.chmod(0o600)

        refused = self.run_transaction(
            "apply", "--plan", str(plan_file), "--approve-hash", "f" * 64,
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse(target.exists())

        applied = self.run_transaction(
            "apply", "--plan", str(plan_file), "--approve-hash",
            plan["approval_sha256"],
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        evidence = json.loads(applied.stdout)
        self.assertEqual(evidence["status"], "applied")
        self.assertTrue(all((target / tool).is_symlink() for tool in (
            "node", "npm", "npx",
        )))
        checked = self.run_transaction(
            "check", "--journal", str(target.parent / "runtime-pin-journal.json"),
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(json.loads(checked.stdout)["status"], "ready")

        replay = self.run_transaction(
            "apply", "--plan", str(plan_file), "--approve-hash",
            plan["approval_sha256"],
        )
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertEqual(json.loads(replay.stdout)["status"], "replayed")

        (target / "node").unlink()
        (target / "node").symlink_to(self.system / "node")
        drift = self.run_transaction(
            "apply", "--plan", str(plan_file), "--approve-hash",
            plan["approval_sha256"],
        )
        self.assertNotEqual(drift.returncode, 0)
        self.assertIn("compare-and-swap conflict", drift.stderr)

    def test_project_plan_tamper_is_rejected(self) -> None:
        target = self.home / ".factory/kits/projects/relay/runtime/bin"
        target.parent.mkdir(parents=True)
        planned = self.run_transaction(
            "plan", "--product", str(self.product), "--runtime-bin",
            str(self.runtime), "--target-bin", str(target),
        )
        plan = json.loads(planned.stdout)
        plan["candidate"]["node"]["path"] = str(self.system / "node")
        plan_file = self.home / "tampered-runtime-plan.json"
        plan_file.write_text(json.dumps(plan))
        plan_file.chmod(0o600)
        result = self.run_transaction(
            "apply", "--plan", str(plan_file), "--approve-hash",
            plan["approval_sha256"],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("runtime plan approval hash is invalid", result.stderr)

    def test_project_apply_resumes_interruption_and_serializes_concurrency(self) -> None:
        target = self.home / ".factory/kits/projects/relay/runtime/bin"
        target.parent.mkdir(parents=True)
        planned = self.run_transaction(
            "plan", "--product", str(self.product), "--runtime-bin",
            str(self.runtime), "--target-bin", str(target),
        )
        plan = json.loads(planned.stdout)
        plan_file = self.home / "recoverable-runtime-plan.json"
        plan_file.write_text(planned.stdout)
        plan_file.chmod(0o600)
        arguments = (
            "apply", "--plan", str(plan_file), "--approve-hash",
            plan["approval_sha256"],
        )
        interrupted = self.run_transaction(
            *arguments, environment={
                "FACTORY_RUNTIME_PIN_TEST_MODE": "1",
                "FACTORY_RUNTIME_PIN_TEST_FAIL_AFTER_TOOL": "node",
            },
        )
        self.assertNotEqual(interrupted.returncode, 0)
        self.assertIn("injected runtime pin interruption", interrupted.stderr)
        self.assertTrue((target / "node").is_symlink())
        self.assertFalse((target / "npm").exists())
        resumed = self.run_transaction(*arguments)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(json.loads(resumed.stdout)["status"], "applied")

        env = os.environ.copy()
        env.update({"HOME": str(self.home), "PATH": f"{self.system}:{env['PATH']}"})
        command = ["python3", str(PIN), *arguments]
        workers = [
            subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, env=env)
            for _ in range(2)
        ]
        results = [worker.communicate(timeout=20) + (worker.returncode,) for worker in workers]
        self.assertEqual([result[2] for result in results], [0, 0], results)
        self.assertEqual(
            [json.loads(result[0])["status"] for result in results],
            ["replayed", "replayed"],
        )


if __name__ == "__main__":
    unittest.main()
