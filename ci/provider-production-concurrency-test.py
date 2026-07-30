#!/usr/bin/env python3
"""Focused production subscription-concurrency regression tests."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "scripts/provider-concurrency-config.py"
COORDINATOR = ROOT / "scripts/provider-coordinator.py"
DOCTOR = ROOT / "scripts/factory-doctor.sh"
RUN_AGENT = ROOT / "scripts/run-agent.sh"
CLI_RUNTIME = ROOT / "scripts/provider-cli-runtime.py"
CODEX_ADAPTER = ROOT / "scripts/adapters/codex.sh"


class ProductionConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="pc.", dir="/tmp")
        self.root = Path(self.temporary.name).resolve()
        os.chmod(self.root, 0o700)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        credentials = {
            self.home / ".claude/.credentials.json": b'{"claude":"credential"}\n',
            self.home / ".codex/auth.json": b'{"codex":"credential"}\n',
            self.home / ".cursor/auth.json": b'{"cursor":"credential"}\n',
            self.home / ".cursor/cli-config.json": b'{"version":1}\n',
        }
        for path, content in credentials.items():
            path.parent.mkdir(mode=0o700, exist_ok=True)
            path.write_bytes(content)
            os.chmod(path, 0o600)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def command(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(CONFIG),
                "--release",
                str(ROOT),
                "--root",
                str(self.state),
                "--capacity",
                "3",
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=check,
            timeout=60,
        )

    def apply(self) -> dict:
        preview = json.loads(self.command("plan").stdout)
        return json.loads(
            self.command("apply", "--approve-hash", preview["approval_sha256"]).stdout
        )

    def coordinator(self, *arguments: str) -> dict:
        result = subprocess.run(
            [
                sys.executable,
                str(COORDINATOR),
                "--db",
                str(self.state / "accounting/state-v2.sqlite3"),
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
        return json.loads(result.stdout)

    def reserve(self, attempt: str, family: str, account: str) -> dict:
        return self.coordinator(
            "reserve",
            "--operation-id",
            attempt,
            "--attempt-id",
            attempt,
            "--provider-family",
            family,
            "--account-route",
            account,
            "--reserve-micro-usd",
            "1",
            "--product-id",
            "product",
            "--ticket-id",
            f"T-{attempt[-1]}",
            "--budget-day",
            "2026-07-29",
            "--product-daily-cap-micro-usd",
            "10",
            "--ticket-cap-micro-usd",
            "10",
            "--machine-daily-cap-micro-usd",
            "10",
            "--policy",
            str(self.state / "provider-policy.json"),
            "--configuration-lock",
            str(self.state / "provider-configuration.lock"),
        )

    def terminalize(self, attempt: str, version: int = 2) -> None:
        self.coordinator(
            "terminalize",
            "--operation-id",
            f"{attempt}-terminal",
            "--attempt-id",
            attempt,
            "--expected-version",
            str(version),
            "--result",
            "succeeded",
            "--charge-micro-usd",
            "0",
        )

    def prepare_runtime(self, adapter: str, attempt: str) -> Path:
        script = f"""
set -euo pipefail
eval "$(sed -n '/^copy_cli_credential()/,/^}}/p;
  /^prepare_cli_runtime()/,/^}}/p' '{RUN_AGENT}')"
CLI_CONCURRENT_RUN=1
CLI_RUNTIME_STATE_ROOT='{self.state}/cli-runtimes'
CLI_RUNTIME_LAYOUT=owner
FACTORY_CLAUDE_SETTINGS=
FACTORY_CURSOR_SESSION_HOME='{self.home}'
HOME='{self.home}'
ADAPTER='{adapter}'
CLI_ATTEMPT_ID='{attempt}'
CLI_RUNTIME_ROOT=
CLI_PROVIDER_HOME=
CLI_PROVIDER_TMPDIR=
CLI_CLAUDE_CONFIG_DIR=
CLI_CLAUDE_SETTINGS=
CLI_CURSOR_CONFIG_DIR=
CLI_CURSOR_DATA_DIR=
prepare_cli_runtime
printf '%s\\n' "$CLI_RUNTIME_ROOT"
"""
        result = subprocess.run(
            ["/bin/bash", "-c", script],
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
        return Path(result.stdout.strip())

    def cleanup_runtime(self, adapter: str, attempt: str, runtime: Path) -> None:
        script = f"""
set -euo pipefail
eval "$(sed -n '/^cleanup_cli_runtime()/,/^}}/p' '{RUN_AGENT}')"
CLI_RUNTIME_ROOT='{runtime}'
CLI_RUNTIME_STATE_ROOT='{self.state}/cli-runtimes'
CLI_RUNTIME_LAYOUT=owner
DEVELOPMENT_LANE_ROOT=
CLI_ATTEMPT_ID='{attempt}'
ADAPTER='{adapter}'
RUN_GROUP_TERMINATED=1
cleanup_cli_runtime
"""
        subprocess.run(
            ["/bin/bash", "-c", script],
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )

    def test_configuration_covers_three_clis_and_admits_cross_and_same_route(self) -> None:
        missing = self.command("check", check=False)
        self.assertEqual(missing.returncode, 2)
        ready = self.apply()
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(
            ready["adapters"],
            ["claude-code", "codex", "cursor-anthropic", "cursor-openai"],
        )
        self.assertEqual(ready["release_path"], str(ROOT))
        self.assertEqual(
            ready["runtime_root"]["path"], str(self.state / "cli-runtimes")
        )
        self.assertEqual(ready["runtime_root"]["mode"], "0700")
        self.assertEqual(ready["capacity"], 3)
        self.assertEqual(ready["required_capacity"], 3)
        self.assertEqual(ready["policy_capacities"]["coupled"], 3)
        configuration_lock = self.state / "provider-configuration.lock"
        self.assertTrue(configuration_lock.is_file())
        self.assertEqual(stat.S_IMODE(configuration_lock.stat().st_mode), 0o600)
        default_activation = self.state / "isolated-v1.enabled"
        qualification_activation = self.state / "provider-activation.json"
        default_activation.rename(qualification_activation)
        self.assertEqual(
            json.loads(
                self.command(
                    "check", "--activation", str(qualification_activation)
                ).stdout
            )["status"],
            "ready",
        )
        qualification_activation.rename(default_activation)
        for attempt, family, account in (
            ("cross-1", "openai", "codex-native"),
            ("cross-2", "anthropic", "claude-native"),
            ("cross-3", "openai", "cursor"),
        ):
            self.assertTrue(self.reserve(attempt, family, account)["admitted"])
        for attempt in ("cross-1", "cross-2", "cross-3"):
            self.terminalize(attempt)
        for attempt in ("cursor-1", "cursor-2", "cursor-3"):
            self.assertTrue(self.reserve(attempt, "openai", "cursor")["admitted"])
        for attempt in ("cursor-1", "cursor-2", "cursor-3"):
            self.terminalize(attempt)
        activation_path = self.state / "isolated-v1.enabled"
        activation = json.loads(activation_path.read_text())
        activation["routes"]["unknown-route"] = {
            "account_route": "cursor",
            "adapter": "cursor-openai",
            "model": "unknown",
            "provider_family": "openai",
        }
        activation_path.write_text(
            json.dumps(
                activation, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        )
        self.assertEqual(self.command("check", check=False).returncode, 2)

    def test_configuration_lock_serializes_apply_and_reservation(self) -> None:
        self.apply()
        lock_path = self.state / "provider-configuration.lock"
        descriptor = os.open(lock_path, os.O_RDWR)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        result: dict[str, object] = {}

        def reserve() -> None:
            result.update(self.reserve("locked-1", "openai", "codex-native"))

        worker = threading.Thread(target=reserve)
        worker.start()
        time.sleep(0.15)
        self.assertTrue(worker.is_alive())
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(result["admitted"])
        self.terminalize("locked-1")

    def test_check_refuses_unapproved_policy_tuning_drift(self) -> None:
        self.apply()
        policy_path = self.state / "provider-policy.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["global"]["max_starts"] += 1
        policy_path.write_text(
            json.dumps(
                policy, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ) + "\n",
            encoding="utf-8",
        )
        activation_path = self.state / "isolated-v1.enabled"
        activation = json.loads(activation_path.read_text(encoding="utf-8"))
        activation["policy_sha256"] = hashlib.sha256(
            json.dumps(
                policy, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        activation_path.write_text(
            json.dumps(
                activation, ensure_ascii=True, sort_keys=True,
                separators=(",", ":"),
            ) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(self.command("check", check=False).returncode, 2)

    def test_all_cli_adapters_receive_separate_owner_local_runtime(self) -> None:
        self.apply()
        script = f"""
set -euo pipefail
eval "$(sed -n '/^copy_cli_credential()/,/^}}/p;
  /^prepare_cli_runtime()/,/^}}/p' '{RUN_AGENT}')"
CLI_CONCURRENT_RUN=1
CLI_RUNTIME_STATE_ROOT='{self.state}/cli-runtimes'
CLI_RUNTIME_LAYOUT=owner
FACTORY_CLAUDE_SETTINGS=
FACTORY_CURSOR_SESSION_HOME='{self.home}'
HOME='{self.home}'
for entry in codex:prod-codex claude-code:prod-claude cursor-openai:prod-cursor; do
  ADAPTER="${{entry%%:*}}"
  CLI_ATTEMPT_ID="${{entry#*:}}"
  CLI_RUNTIME_ROOT=
  CLI_PROVIDER_HOME=
  CLI_PROVIDER_TMPDIR=
  CLI_CLAUDE_CONFIG_DIR=
  CLI_CLAUDE_SETTINGS=
  CLI_CURSOR_CONFIG_DIR=
  CLI_CURSOR_DATA_DIR=
  prepare_cli_runtime
  printf '%s\\t%s\\n' "$ADAPTER" "$CLI_RUNTIME_ROOT"
done
"""
        result = subprocess.run(
            ["/bin/bash", "-c", script],
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
        runtimes = dict(line.split("\t") for line in result.stdout.splitlines())
        self.assertEqual(len(set(runtimes.values())), 3)
        expected = {
            "codex": ("home/.codex/auth.json", b'{"codex":"credential"}\n'),
            "claude-code": ("config/.credentials.json", b'{"claude":"credential"}\n'),
            "cursor-openai": ("home/.cursor/auth.json", b'{"cursor":"credential"}\n'),
        }
        for adapter, (relative, content) in expected.items():
            runtime = Path(runtimes[adapter])
            credential = runtime / relative
            self.assertEqual(credential.read_bytes(), content)
            self.assertEqual(stat.S_IMODE(credential.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(runtime.stat().st_mode), 0o700)
            for name in ("cache", "output", "tmp"):
                namespace = runtime / name
                self.assertTrue(namespace.is_dir())
                self.assertEqual(stat.S_IMODE(namespace.stat().st_mode), 0o700)
        cursor_copy = Path(runtimes["cursor-openai"]) / "home/.cursor/cli-config.json"
        cursor_copy.write_text('{"changed":true}\n')
        self.assertEqual(
            (self.home / ".cursor/cli-config.json").read_text(),
            '{"version":1}\n',
        )

    def test_three_distinct_cli_routes_overlap_then_drain_independently(self) -> None:
        self.apply()
        routes = (
            ("codex", "live-codex", "openai", "codex-native"),
            ("claude-code", "live-claude", "anthropic", "claude-native"),
            ("cursor-openai", "live-cursor", "openai", "cursor"),
        )
        runtimes = {
            attempt: self.prepare_runtime(adapter, attempt)
            for adapter, attempt, _family, _account in routes
        }
        markers = self.root / "markers"
        markers.mkdir()
        command = (
            "import pathlib,sys,time;"
            "p=pathlib.Path(sys.argv[1]);p.touch();"
            "deadline=time.monotonic()+5;"
            "\nwhile len(list(p.parent.glob('ready-*'))) < 3:"
            "\n  if time.monotonic() >= deadline: raise SystemExit(9)"
            "\n  time.sleep(.02)"
            "\nwhile not (p.parent/'release').exists():"
            "\n  if time.monotonic() >= deadline: raise SystemExit(10)"
            "\n  time.sleep(.02)"
        )
        processes: dict[str, subprocess.Popen[str]] = {}
        for index, (adapter, attempt, family, account) in enumerate(routes, 1):
            runtime = runtimes[attempt]
            environment = {
                **os.environ,
                "HOME": str(runtime / "home"),
                "TMPDIR": str(runtime / "tmp"),
            }
            processes[attempt] = subprocess.Popen(
                [
                    sys.executable,
                    str(CLI_RUNTIME),
                    "--coordinator",
                    str(COORDINATOR),
                    "--db",
                    str(self.state / "accounting/state-v2.sqlite3"),
                    "--policy",
                    str(self.state / "provider-policy.json"),
                    "--configuration-lock",
                    str(self.state / "provider-configuration.lock"),
                    "--attempt-id",
                    attempt,
                    "--provider-family",
                    family,
                    "--account-route",
                    account,
                    "--reserve-micro-usd",
                    "1",
                    "--product-id",
                    "product",
                    "--ticket-id",
                    f"T-{index}",
                    "--budget-day",
                    "2026-07-29",
                    "--product-cap-micro-usd",
                    "3",
                    "--ticket-cap-micro-usd",
                    "1",
                    "--machine-cap-micro-usd",
                    "3",
                    "--",
                    sys.executable,
                    "-c",
                    command,
                    str(markers / f"ready-{attempt}"),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
        deadline = time.monotonic() + 5
        active: list[dict] = []
        while time.monotonic() < deadline:
            status = self.coordinator("status")
            active = [
                attempt
                for attempt in status["attempts"]
                if attempt["state"] == "submitted"
            ]
            if (
                len(active) == 3
                and len(list(markers.glob("ready-*"))) == 3
                and all(process.poll() is None for process in processes.values())
            ):
                break
            time.sleep(0.02)
        self.assertEqual(
            {(item["provider_family"], item["account_route"]) for item in active},
            {
                ("openai", "codex-native"),
                ("anthropic", "claude-native"),
                ("openai", "cursor"),
            },
        )
        self.assertEqual(len(list(markers.glob("ready-*"))), 3)
        self.assertTrue(all(process.poll() is None for process in processes.values()))
        (markers / "release").touch()
        results = {
            attempt: process.communicate(timeout=10)
            for attempt, process in processes.items()
        }
        self.assertTrue(
            all(process.returncode == 0 for process in processes.values()),
            results,
        )
        remaining = set(runtimes)
        for adapter, attempt, _family, _account in routes:
            self.terminalize(attempt, version=4)
            self.cleanup_runtime(adapter, attempt, runtimes[attempt])
            remaining.remove(attempt)
            self.assertFalse(runtimes[attempt].exists())
            self.assertTrue(all(runtimes[item].is_dir() for item in remaining))
            status = self.coordinator("status")
            self.assertEqual(
                sum(
                    item["state"] in {"reserved", "GO", "submitted"}
                    for item in status["attempts"]
                ),
                len(remaining),
            )

    def test_codex_adapter_uses_the_attempt_local_home(self) -> None:
        self.apply()
        runtime = self.prepare_runtime("codex", "adapter-codex")
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        fake_codex = fake_bin / "codex"
        fake_codex.write_text(
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = --version ]; then\n"
            "  echo 'codex-cli 0.144.1'\n"
            "else\n"
            "  printf '%s\\n' "
            "'{\"input_tokens\":10,\"output_tokens\":4}'\n"
            "fi\n"
        )
        fake_codex.chmod(0o700)
        environment = {
            **os.environ,
            "CODEX_PINNED": "0.144.1",
            "FACTORY_CLI_ATTEMPT_ID": "adapter-codex",
            "FACTORY_CLI_INTERNAL_SANDBOX": "1",
            "FACTORY_TIMEOUT_FOREGROUND": "1",
            "HOME": str(runtime / "home"),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "TMPDIR": str(runtime / "tmp"),
        }
        result = subprocess.run(
            [
                str(CODEX_ADAPTER),
                "--budget",
                "1",
                "--max-turns",
                "1",
                "--timeout-min",
                "1",
                "--prompt-file",
                "/dev/null",
                "--workdir",
                str(self.root),
                "--model",
                "test",
                "--effort",
                "low",
                "--",
                "mock task",
            ],
            text=True,
            capture_output=True,
            check=False,
            env=environment,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"input_tokens":10', result.stdout)
        self.cleanup_runtime("codex", "adapter-codex", runtime)
        self.assertFalse(runtime.exists())

    def test_doctor_fails_closed_until_multi_ticket_provider_is_ready(self) -> None:
        product = self.root / "product"
        (product / "factory").mkdir(parents=True)
        sha = "1" * 40
        (product / "factory/KIT_PIN").write_text(sha + "\n")
        (product / "factory/PROJECT.env").write_text("MAX_CONCURRENT_TICKETS=3\n")
        environment = {
            **os.environ,
            "FACTORY_PROVIDER_ACTIVATION": str(self.state / "isolated-v1.enabled"),
            "FACTORY_PROVIDER_APPLY_LOCK_ROOT": str(self.state / "provider-apply-locks"),
            "FACTORY_PROVIDER_ATTEMPT_ROOT": str(self.state / "provider-attempts"),
            "FACTORY_PROVIDER_DB": str(self.state / "accounting/state-v2.sqlite3"),
            "FACTORY_PROVIDER_POLICY": str(self.state / "provider-policy.json"),
            "FACTORY_RELEASE_CONTRACT_VERSION": "1.8.0",
            "HOME": str(self.home),
        }
        arguments = [
            str(DOCTOR),
            "--json",
            "--kit-dir",
            str(ROOT),
            "--product-root",
            str(product),
            "--kit-sha",
            sha,
        ]
        missing = subprocess.run(
            arguments,
            text=True,
            capture_output=True,
            check=False,
            env=environment,
            timeout=30,
        )
        self.assertEqual(missing.returncode, 1)
        self.assertFalse(
            json.loads(missing.stdout)["checks"]["isolated_provider"][
                "concurrency_ready"
            ]
        )
        self.apply()
        ready = subprocess.run(
            arguments,
            text=True,
            capture_output=True,
            check=False,
            env=environment,
            timeout=30,
        )
        self.assertEqual(ready.returncode, 0, ready.stdout + ready.stderr)
        provider = json.loads(ready.stdout)["checks"]["isolated_provider"]
        self.assertTrue(provider["concurrency_required"])
        self.assertTrue(provider["concurrency_ready"])


if __name__ == "__main__":
    unittest.main()
