#!/usr/bin/env python3
"""Fail-closed tests for owner-local provider activation."""

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

    def command(self, route="route-a", contract="1.6.0", status=False):
        selection = ["--status"] if status else ["--route-id", route]
        return subprocess.run(
            [
                sys.executable, str(HELPER),
                "--config", str(self.config),
                "--contract-version", contract,
                *selection,
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
        missing_value = json.loads(missing.stdout)
        self.assertEqual(missing_value["status"], "disabled")
        self.assertEqual(
            missing_value["schema"],
            "nysa.software-factory.provider-activation-selection/v1",
        )

    def test_contract_1_7_selects_exact_cli_route_and_policy(self):
        self.value = {
            "enabled": True,
            "mode": "cli-concurrent-v1",
            "policy_sha256": "a" * 64,
            "routes": {
                "route-a": {
                    "account_route": "codex-native",
                    "adapter": "codex",
                    "model": "gpt-5.6-sol",
                    "provider_family": "openai",
                }
            },
            "schema": "nysa.software-factory.provider-activation/v2",
        }
        self.write()
        selected = self.command(contract="1.7.0")
        self.assertEqual(selected.returncode, 0, selected.stdout + selected.stderr)
        value = json.loads(selected.stdout)
        self.assertEqual(value["execution_mode"], "cli-concurrent-v1")
        self.assertEqual(value["policy_sha256"], "a" * 64)
        self.assertEqual(value["adapter"], "codex")
        status = self.command(contract="1.7.0", status=True)
        self.assertEqual(status.returncode, 0)
        self.assertEqual(
            json.loads(status.stdout)["execution_mode"], "cli-concurrent-v1"
        )

    def test_contract_1_6_rejects_cli_activation(self):
        self.value = {
            "enabled": True,
            "mode": "cli-concurrent-v1",
            "policy_sha256": "a" * 64,
            "routes": {},
            "schema": "nysa.software-factory.provider-activation/v2",
        }
        self.write()
        refused = self.command(contract="1.6.0")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("does not support CLI activation", refused.stdout)

    def test_cli_activation_rejects_invalid_mode_policy_and_route(self):
        self.value = {
            "enabled": True,
            "mode": "cli-concurrent-v1",
            "policy_sha256": "a" * 64,
            "routes": {
                "route-a": {
                    "account_route": "codex-native",
                    "adapter": "codex",
                    "model": "gpt-5.6-sol",
                    "provider_family": "openai",
                }
            },
            "schema": "nysa.software-factory.provider-activation/v2",
        }
        for field, invalid in (("mode", "isolated-v1"), ("policy_sha256", "A" * 64)):
            self.value[field] = invalid
            self.write()
            self.assertEqual(self.command(contract="1.7.0").returncode, 2)
            self.value[field] = "cli-concurrent-v1" if field == "mode" else "a" * 64
        self.value["routes"]["route-a"]["adapter"] = "../codex"
        self.write()
        self.assertEqual(self.command(contract="1.7.0").returncode, 2)
        self.value["routes"]["route-a"]["adapter"] = "future-cli"
        self.write()
        self.assertEqual(self.command(contract="1.7.0").returncode, 2)

    def test_cli_activation_rejects_invalid_unselected_route(self):
        self.value = {
            "enabled": True,
            "mode": "cli-concurrent-v1",
            "policy_sha256": "a" * 64,
            "routes": {
                "route-a": {
                    "account_route": "codex-native",
                    "adapter": "codex",
                    "model": "gpt-5.6-sol",
                    "provider_family": "openai",
                },
                "route-b": {
                    "account_route": "cursor",
                    "adapter": "untrusted-cli",
                    "model": "model-b",
                    "provider_family": "openai",
                },
            },
            "schema": "nysa.software-factory.provider-activation/v2",
        }
        self.write()
        self.assertEqual(self.command(contract="1.7.0").returncode, 2)

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
