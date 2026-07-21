#!/usr/bin/env python3
"""Fail-closed tests for owner-local isolated-v1 activation."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "provider-activation.py"


class ActivationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.config = self.root / "activation.json"
        self.value = {
            "enabled": True,
            "routes": {
                "route-a": {
                    "account_route": "account-a",
                    "broker_path": "/v1/messages",
                    "model": "model-a",
                    "protocol": "anthropic-messages",
                    "provider_family": "anthropic",
                }
            },
            "schema": "nysa.software-factory.provider-activation/v1",
        }
        self.write()

    def tearDown(self):
        self.temp.cleanup()

    def write(self):
        self.config.write_text(
            json.dumps(self.value, sort_keys=True, separators=(",", ":")) + "\n"
        )
        os.chmod(self.config, 0o600)

    def command(self, route="route-a"):
        return subprocess.run(
            [
                sys.executable, str(HELPER),
                "--config", str(self.config),
                "--route-id", route,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_exact_owner_activation_selects_only_configured_route(self):
        selected = self.command()
        self.assertEqual(selected.returncode, 0, selected.stdout + selected.stderr)
        value = json.loads(selected.stdout)
        self.assertEqual(value["model"], "model-a")
        self.assertEqual(value["status"], "enabled")
        missing = self.command("route-b")
        self.assertEqual(missing.returncode, 2)
        self.assertEqual(json.loads(missing.stdout)["status"], "disabled")

    def test_permissions_disabled_flag_and_noncanonical_data_fail_closed(self):
        os.chmod(self.config, 0o644)
        self.assertEqual(self.command().returncode, 2)
        self.write()
        self.value["enabled"] = False
        self.write()
        self.assertEqual(self.command().returncode, 2)
        self.value["enabled"] = True
        self.config.write_text(json.dumps(self.value, indent=2))
        os.chmod(self.config, 0o600)
        self.assertEqual(self.command().returncode, 2)


if __name__ == "__main__":
    unittest.main()
