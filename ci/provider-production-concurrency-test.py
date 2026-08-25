#!/usr/bin/env python3
"""Focused production subscription-concurrency regression tests."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import sqlite3
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
BACKEND_POLICY = ROOT / "scripts/lib/backend-policy.sh"


class ProductionConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="pc.", dir="/tmp")
        self.root = Path(self.temporary.name).resolve()
        os.chmod(self.root, 0o700)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.owner_start = " ".join(subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(os.getpid())], text=True
        ).split())
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

    def cursor_account(
        self, action: str, lease: str, scope: str = "production-certified"
    ) -> dict:
        arguments = [
            "--account-db", str(self.state / "accounting/cursor-account.sqlite3"),
            f"account-{action}", "--lease-id", lease,
            "--owner-pid", str(os.getpid()),
            "--owner-pgid", str(os.getpgrp()),
            "--owner-start", self.owner_start,
        ]
        if action == "acquire":
            activation = json.loads(
                (self.state / "isolated-v1.enabled").read_text(encoding="utf-8")
            )
            arguments.extend([
                "--account-route", "cursor",
                "--trust-scope", scope,
                "--policy", str(self.state / "provider-policy.json"),
                "--configuration-lock",
                str(self.state / "provider-configuration.lock"),
                "--expected-policy-sha256", activation["policy_sha256"],
                "--wait-seconds", "2",
            ])
        elif action == "bind-runtime":
            runtime = os.getpgrp()
            runtime_start = " ".join(subprocess.check_output(
                ["ps", "-o", "lstart=", "-p", str(runtime)], text=True
            ).split())
            arguments.extend([
                "--runtime-pid", str(runtime), "--runtime-pgid", str(runtime),
                "--runtime-start", runtime_start,
            ])
        return self.coordinator(*arguments)

    def test_development_cursor_account_scope_is_explicit(self) -> None:
        self.apply()
        lease = "development-local"
        admitted = self.cursor_account(
            "acquire", lease, scope="development-local"
        )
        self.assertTrue(admitted["admitted"])
        self.assertEqual(admitted["lease"]["trust_scope"], "development-local")
        self.assertTrue(self.cursor_account("release", lease)["released"])

    def test_cursor_account_response_loss_cleanup_is_exact_and_idempotent(self) -> None:
        self.apply()
        lease = "response-loss-account"
        self.assertTrue(self.cursor_account("acquire", lease)["admitted"])
        script = f"""
set -euo pipefail
eval "$(sed -n '/^release_cursor_account_lease()/,/^}}/p' '{RUN_AGENT}')"
CURSOR_ACCOUNT_LEASE_ACTIVE=1
CURSOR_ACCOUNT_LEASE_ID='{lease}'
CURSOR_ACCOUNT_OWNER_PID='{os.getpid()}'
CURSOR_ACCOUNT_OWNER_PGID='{os.getpgrp()}'
CURSOR_ACCOUNT_OWNER_START='{self.owner_start}'
FACTORY_PROVIDER_DB='{self.state}/accounting/state-v2.sqlite3'
FACTORY_CURSOR_ACCOUNT_DB='{self.state}/accounting/cursor-account.sqlite3'
KIT_DIR='{ROOT}'
release_cursor_account_lease
[[ "$CURSOR_ACCOUNT_LEASE_ACTIVE" == 0 ]]
CURSOR_ACCOUNT_LEASE_ACTIVE=1
release_cursor_account_lease
[[ "$CURSOR_ACCOUNT_LEASE_ACTIVE" == 0 ]]
"""
        result = subprocess.run(
            ["/bin/bash", "-c", script], text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            self.coordinator(
                "--account-db", str(self.state / "accounting/cursor-account.sqlite3"),
                "account-status",
            )["leases"],
            [],
        )
        source = RUN_AGENT.read_text(encoding="utf-8")
        block = source.index('if [[ "$CLI_CONCURRENT_RUN" -eq 1 && "$ADAPTER" == cursor-* ]]')
        ownership = source.index("CURSOR_ACCOUNT_LEASE_ACTIVE=1", block)
        self.assertLess(
            ownership,
            source.index('--account-db "$FACTORY_CURSOR_ACCOUNT_DB" account-acquire', block),
        )

    def test_submission_capture_before_attempt_creation_is_a_noop(self) -> None:
        script = f"""
set -euo pipefail
eval "$(sed -n '/^capture_submission_record()/,/^}}/p' '{RUN_AGENT}')"
CLI_CONCURRENT_RUN=1
CLI_ATTEMPT_ID=
RUN_SUBMITTED_FILE=
load_cli_attempt() {{ return 99; }}
capture_submission_record
"""
        result = subprocess.run(
            ["/bin/bash", "-c", script], capture_output=True, check=False,
            text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_failed_reserve_cannot_terminalize_conflicting_attempt(self) -> None:
        self.apply()
        attempt = "foreign-attempt"
        original = self.reserve(attempt, "openai", "codex-native")["attempt"]
        conflict = subprocess.run(
            [
                sys.executable, str(COORDINATOR), "--db",
                str(self.state / "accounting/state-v2.sqlite3"), "reserve",
                "--operation-id", f"{attempt}-conflict", "--attempt-id", attempt,
                "--provider-family", "openai", "--account-route", "codex-native",
                "--reserve-micro-usd", "1", "--product-id", "wanted-product",
                "--ticket-id", "T-wanted", "--budget-day", "2026-07-29",
                "--product-daily-cap-micro-usd", "10",
                "--ticket-cap-micro-usd", "10",
                "--machine-daily-cap-micro-usd", "10", "--policy",
                str(self.state / "provider-policy.json"), "--configuration-lock",
                str(self.state / "provider-configuration.lock"),
            ],
            text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(conflict.returncode, 0)
        policy_hash = json.loads(
            (self.state / "isolated-v1.enabled").read_text(encoding="utf-8")
        )["policy_sha256"]
        script = f"""
set -euo pipefail
eval "$(sed -n '/^load_cli_attempt()/,/^release_cursor_account_lease()/p' \
  '{RUN_AGENT}' | sed '$d')"
CLI_ATTEMPT_ACTIVE=1
CLI_ATTEMPT_ID='{attempt}'
FACTORY_PROVIDER_DB='{self.state}/accounting/state-v2.sqlite3'
KIT_DIR='{ROOT}'
SELECTED_FAMILY=openai
SELECTED_ACCOUNT_ROUTE_ID=codex-native
PROVIDER_BUDGET_MICRO_VALUES=(1 10 10 10)
CLI_PRODUCT_ID=wanted-product
TICKET=T-wanted
TODAY=2026-07-29
ACTIVATED_POLICY_HASH='{policy_hash}'
accounting_intent_is_durable() {{ return 1; }}
! reconcile_cli_attempt failed
[[ "$CLI_ATTEMPT_ACTIVE" == 1 ]]
"""
        cleanup = subprocess.run(
            ["/bin/bash", "-c", script], text=True, capture_output=True, check=False,
        )
        self.assertEqual(cleanup.returncode, 0, cleanup.stdout + cleanup.stderr)
        current = self.coordinator("status", "--attempt-id", attempt)["attempts"][0]
        self.assertEqual(current, {key: value for key, value in original.items() if key != "schema"})

    def test_cleanup_refuses_go_attempt_with_foreign_policy(self) -> None:
        self.apply()
        attempt = "foreign-policy"
        reserved = self.reserve(attempt, "openai", "codex-native")["attempt"]
        self.coordinator(
            "mark-go", "--operation-id", f"{attempt}-go", "--attempt-id", attempt,
            "--expected-version", str(reserved["version"]),
        )
        database = self.state / "accounting/state-v2.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE attempts SET policy_sha256=? WHERE attempt_id=?",
                ("f" * 64, attempt),
            )
        policy_hash = json.loads(
            (self.state / "isolated-v1.enabled").read_text(encoding="utf-8")
        )["policy_sha256"]
        script = f"""
set -euo pipefail
eval "$(sed -n '/^load_cli_attempt()/,/^release_cursor_account_lease()/p' \
  '{RUN_AGENT}' | sed '$d')"
CLI_ATTEMPT_ACTIVE=1
CLI_ATTEMPT_ID='{attempt}'
FACTORY_PROVIDER_DB='{database}'
KIT_DIR='{ROOT}'
SELECTED_FAMILY=openai
SELECTED_ACCOUNT_ROUTE_ID=codex-native
PROVIDER_BUDGET_MICRO_VALUES=(1 10 10 10)
CLI_PRODUCT_ID=product
TICKET=T-y
TODAY=2026-07-29
ACTIVATED_POLICY_HASH='{policy_hash}'
accounting_intent_is_durable() {{ return 1; }}
! reconcile_cli_attempt failed
[[ "$CLI_ATTEMPT_ACTIVE" == 1 ]]
"""
        cleanup = subprocess.run(
            ["/bin/bash", "-c", script], text=True, capture_output=True, check=False,
        )
        self.assertEqual(cleanup.returncode, 0, cleanup.stdout + cleanup.stderr)
        current = self.coordinator("status", "--attempt-id", attempt)["attempts"][0]
        self.assertEqual(current["state"], "GO")
        self.assertEqual(current["policy_sha256"], "f" * 64)

    def test_terminalization_uses_actual_charge_below_reservation(self) -> None:
        script = f"""
set -euo pipefail
eval "$(sed -n '/^micro_usd()/,/^}}/p;
  /^finalize_accounting()/,/^}}/p;
  /^accounting_intent_is_durable()/,/^}}/p;
  /^seal_cleanup_accounting_intent()/,/^}}/p;
  /^finalize_cleanup_accounting()/,/^}}/p;
  /^accounting_cleanup_required()/,/^}}/p;
  /^reconcile_cli_attempt()/,/^}}/p' '{RUN_AGENT}')"
actual="$(micro_usd 1.250000)"
[[ "$actual" == 1250000 && "$(micro_usd 0.00000001)" == 1 ]]
CLI_ATTEMPT_ACTIVE=1
TASK_SUBMITTED=0
CLI_ATTEMPT_ID=actual-charge
CLI_ATTEMPT_STATE=submitted
CLI_ATTEMPT_VERSION=4
FACTORY_PROVIDER_DB='{self.state}/accounting/state-v2.sqlite3'
KIT_DIR='{ROOT}'
PROVIDER_BUDGET_MICRO_VALUES=(2000000)
load_cli_attempt() {{
  CLI_ATTEMPT_STATE="${{TEST_CLI_STATE:-submitted}}"
  CLI_ATTEMPT_VERSION=4
  CLI_ATTEMPT_TERMINAL_RESULT="${{TEST_TERMINAL_RESULT:-none}}"
  CLI_ATTEMPT_TERMINAL_CHARGE_MICRO="${{TEST_TERMINAL_CHARGE:-none}}"
}}
python3() {{ arguments="$*"; }}
reconcile_cli_attempt succeeded "$actual"
[[ "$arguments" == *'--charge-micro-usd 1250000'* ]]
CLI_ATTEMPT_ACTIVE=1
CLI_TERMINAL_CHARGE_MICRO="$actual"
CLI_TERMINAL_RESULT=succeeded
FINAL_ACCOUNTING_STATE=completed
FINAL_PHASE=completed
MANIFEST_PHASE=terminalizing
reconcile_cli_attempt failed
[[ "$arguments" == *'--charge-micro-usd 1250000'* ]]
CLI_ATTEMPT_ACTIVE=1
TEST_CLI_STATE=terminal
TEST_TERMINAL_RESULT=succeeded
TEST_TERMINAL_CHARGE=1250000
reconcile_cli_attempt succeeded "$actual"
[[ "$CLI_ATTEMPT_ACTIVE" == 0 ]]
CLI_ATTEMPT_ACTIVE=1
TEST_TERMINAL_RESULT=failed
if reconcile_cli_attempt succeeded "$actual"; then exit 1; fi
[[ "$CLI_ATTEMPT_ACTIVE" == 1 ]]
TEST_CLI_STATE=submitted
unset TEST_TERMINAL_RESULT TEST_TERMINAL_CHARGE
CLI_ATTEMPT_ACTIVE=1
MANIFEST_PHASE=spawned
reconcile_cli_attempt failed
[[ "$arguments" == *'--charge-micro-usd 2000000'* ]]
unset -f python3
CLI_TERMINAL_CHARGE_MICRO="$actual"
FINAL_ACCOUNTING_STATE=completed
FINAL_PHASE=completed
COST=1.250000
TURNS=2
COST_BASIS=estimated_tokens
GO_ISSUED=1
RESERVED_USD=2.000000
MANIFEST=/tmp/fixture.meta
MANIFEST_PHASE=terminalizing
ACCOUNTING_STATE=reserved
write_manifest() {{ return 1; }}
if finalize_accounting completed 1.250000 2 0 estimated_tokens completed; then
  exit 1
fi
[[ "$ACCOUNTING_STATE" == completed && "$MANIFEST_PHASE" == terminalizing ]]
accounting_cleanup_required
write_manifest() {{ MANIFEST_PHASE="$1"; }}
finalize_cleanup_accounting 143
[[ "$ACCOUNTING_STATE" == completed && "$EFFECTIVE_COST" == 1.250000 &&
   "$EXIT_STATUS" == 0 && "$MANIFEST_PHASE" == completed ]]
if accounting_cleanup_required; then exit 1; fi
CLI_TERMINAL_CHARGE_MICRO=
CLI_TERMINAL_RESULT=
ACCOUNTING_STATE=reserved
MANIFEST_PHASE=spawned
finalize_cleanup_accounting 0
[[ "$ACCOUNTING_STATE" == abandoned_conservative &&
   "$EFFECTIVE_COST" == 2.000000 && "$EXIT_STATUS" == 125 &&
   "$MANIFEST_PHASE" == abandoned ]]
CLI_TERMINAL_CHARGE_MICRO=
CLI_TERMINAL_RESULT=
FINAL_ACCOUNTING_STATE=
FINAL_PHASE=
MANIFEST_PHASE=spawned
ACCOUNTING_STATE=reserved
GO_ISSUED=1
TURNS=3
PROVIDER_BUDGET_MICRO_VALUES=(2000000)
write_manifest() {{ MANIFEST_PHASE="$1"; }}
seal_cleanup_accounting_intent 143
accounting_intent_is_durable
[[ "$FINAL_ACCOUNTING_STATE" == cancelled_conservative &&
   "$FINAL_PHASE" == cancelled_conservative && "$COST" == 2.000000 &&
   "$EXIT_STATUS" == 143 && "$CLI_TERMINAL_RESULT" == cancelled ]]
CLI_TERMINAL_CHARGE_MICRO=
CLI_TERMINAL_RESULT=
FINAL_ACCOUNTING_STATE=
FINAL_PHASE=
MANIFEST_PHASE=prepared
ACCOUNTING_STATE=reserved
GO_ISSUED=0
seal_cleanup_accounting_intent 3
accounting_intent_is_durable
[[ "$FINAL_ACCOUNTING_STATE" == launch_void && "$FINAL_PHASE" == abandoned &&
   "$COST" == 0 && "$EXIT_STATUS" == 3 &&
   "$CLI_TERMINAL_RESULT" == failed_pre_go ]]
eval "$(sed -n '/^cleanup()/,/^}}/p' '{RUN_AGENT}' | sed '/exec 8<&-/d')"
set +e
PID_FIXTURE='{self.root}/retained.pid'
RUN_PID_FILE="$PID_FIXTURE"
: >"$RUN_PID_FILE"
RUN_GROUP_TERMINATED=1
CLI_ATTEMPT_ACTIVE=1
ROLE_GUARD_ROOT=
RUN_WRAPPER_FILE=
LEASE_HEARTBEAT_PID=
RUN_READY_FILE=
RUN_GO_FILE=
RUN_GATE_FILE=
RUN_SUBMITTED_FILE=
RUN_OUTPUT_TEMP=
HELD_LEDGER_LOCK=0
HELD_GLOBAL_LOCK=0
GLOBAL_LEDGER_SNAPSHOT=
LOCK_DIR='{self.root}/ledger.lock'
LEGACY_INTERVAL_ACTIVE=0
HELD_PROVIDER_LOCK=0
RETAIN_PROVIDER_LOCK=0
HELD_LAUNCH_LOCK=0
LAUNCH_LOCK='{self.root}/launch.lock'
OWNS_ACTIVE_RUN=0
stop_lease_heartbeat() {{ :; }}
terminate_run_group() {{ RUN_GROUP_TERMINATED=1; }}
release_cursor_account_lease() {{ :; }}
cleanup_cli_runtime() {{ :; }}
capture_submission_record() {{ TASK_SUBMITTED=1; }}
reconcile_cli_attempt() {{ return 1; }}
seal_cleanup_accounting_intent() {{
  [[ "$TASK_SUBMITTED" == 1 ]] || return 1
  MANIFEST_PHASE=terminalizing
  ACCOUNTING_STATE=reserved
  CLI_TERMINAL_RESULT=failed
}}
cleanup || true
[[ -f "$PID_FIXTURE" && "$MANIFEST_PHASE" == terminalizing &&
   "$ACCOUNTING_STATE" == reserved && "$CLI_ATTEMPT_ACTIVE" == 1 ]] || {{ echo first-ordering; exit 1; }}
: >"$RUN_PID_FILE"
CLI_ATTEMPT_ACTIVE=1
MANIFEST_PHASE=spawned
ACCOUNTING_STATE=reserved
ACCOUNTING_SCHEMA=1
HELD_LEDGER_LOCK=1
reconcile_cli_attempt() {{ CLI_ATTEMPT_ACTIVE=0; }}
accounting_cleanup_required() {{ return 0; }}
finalize_cleanup_accounting() {{
  MANIFEST_PHASE=abandoned
  ACCOUNTING_STATE=abandoned_conservative
}}
refresh_runtime_ledger() {{ :; }}
finalize_global_ledger() {{ :; }}
cleanup || true
[[ ! -e "$PID_FIXTURE" && "$MANIFEST_PHASE" == abandoned &&
   "$ACCOUNTING_STATE" == abandoned_conservative && "$CLI_ATTEMPT_ACTIVE" == 0 ]] || {{ echo second-ordering; exit 1; }}
RUN_PID_FILE="$PID_FIXTURE"
: >"$RUN_PID_FILE"
CLI_ATTEMPT_ACTIVE=1
GO_ISSUED=0
TASK_SUBMITTED=0
MANIFEST_PHASE=resolved
ACCOUNTING_STATE=
capture_submission_record() {{
  CLI_ATTEMPT_STATE=terminal
  CLI_ATTEMPT_TERMINAL_RESULT=capacity_denied
  CLI_ATTEMPT_TERMINAL_CHARGE_MICRO=0
}}
reconcile_cli_attempt() {{
  [[ "$1" == capacity_denied && "$2" == 0 ]] || return 1
  CLI_ATTEMPT_ACTIVE=0
}}
finalize_cleanup_accounting() {{
  MANIFEST_PHASE=abandoned
  ACCOUNTING_STATE=launch_void
}}
cleanup || true
[[ ! -e "$PID_FIXTURE" && "$MANIFEST_PHASE" == abandoned &&
   "$ACCOUNTING_STATE" == launch_void && "$CLI_ATTEMPT_ACTIVE" == 0 ]] || {{ echo denial-ordering; exit 1; }}
"""
        result = subprocess.run(
            ["/bin/bash", "-c", script], capture_output=True, check=False,
            text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "CLI_ATTEMPT_ACTIVE=0\n    echo \"CLI provider capacity or budget refused",
            RUN_AGENT.read_text(),
        )
        source = RUN_AGENT.read_text()
        ownership = source.index(
            "CLI_ATTEMPT_ACTIVE=1", source.index("CLI_RESERVATION_ARGS=("),
        )
        self.assertLess(
            ownership, source.index('--db "$FACTORY_PROVIDER_DB" prepare', ownership),
        )
        self.assertLess(
            ownership, source.index('--db "$FACTORY_PROVIDER_DB" reserve', ownership),
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
source '{BACKEND_POLICY}'
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

    def test_claude_runtime_materializes_setup_token(self) -> None:
        token = self.home / ".factory/claude-oauth-token"
        token.parent.mkdir(mode=0o700)
        token_value = "sk-ant-oat01-" + "A" * 80
        token.write_text(token_value + "\n", encoding="utf-8")
        token.chmod(0o600)
        runtime = self.prepare_runtime("claude-code", "setup-token")
        credential = json.loads(
            (runtime / "config/.credentials.json").read_text(encoding="utf-8")
        )["claudeAiOauth"]
        self.assertEqual(credential["accessToken"], token_value)
        self.assertGreater(credential["expiresAt"], int(time.time() * 1000))

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

    def test_check_accepts_database_larger_than_json_limit(self) -> None:
        self.apply()
        database = self.state / "accounting/state-v2.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                ("growth-proof", "x" * 1_100_000),
            )
        self.assertGreater(database.stat().st_size, 1_000_000)
        self.assertEqual(json.loads(self.command("check").stdout)["status"], "ready")

        policy = self.state / "provider-policy.json"
        policy.write_bytes(policy.read_bytes() + b" " * 1_000_000)
        refused = self.command("check", check=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("provider policy is unsafe", refused.stderr)

    def test_check_accepts_a_distinct_short_cli_runtime_root(self) -> None:
        self.apply()
        cli_root = self.root / "q"
        cli_root.mkdir(mode=0o700)
        ready = json.loads(
            self.command("check", "--cli-root", str(cli_root)).stdout
        )
        self.assertEqual(ready["runtime_root"]["path"], str(cli_root))

        too_long = self.root / ("x" * 80)
        too_long.mkdir(mode=0o700)
        refused = self.command(
            "check", "--cli-root", str(too_long), check=False
        )
        self.assertEqual(refused.returncode, 2)
        self.assertIn("too long for isolated Cursor scratch", refused.stderr)

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
source '{BACKEND_POLICY}'
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

    @unittest.skipUnless(sys.platform == "darwin", "macOS zsh login-shell regression")
    def test_login_shell_preserves_certified_task_runtime_and_refuses_drift(self) -> None:
        pinned = self.root / "node-22/bin"
        homebrew = self.root / "homebrew/bin"
        pinned.mkdir(parents=True)
        homebrew.mkdir(parents=True)
        for directory, node, npm in (
            (pinned, "v22.22.0", "10.9.4"),
            (homebrew, "v25.5.0", "11.8.0"),
        ):
            for tool, version in (("node", node), ("npm", npm)):
                path = directory / tool
                path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version}'\n")
                path.chmod(0o755)

        task_path = f"{pinned}:{homebrew}:/usr/bin:/bin:/usr/sbin:/sbin"
        launcher = (ROOT / "scripts/factory-launch").read_text()
        self.assertIn(
            '"FACTORY_CERTIFIED_NODE_VERSION=$ACTIVE_RUNTIME_NODE"', launcher
        )
        self.assertIn(
            '"FACTORY_CERTIFIED_NPM_VERSION=$ACTIVE_RUNTIME_NPM"', launcher
        )
        script = f"""
set -euo pipefail
eval "$(sed -n '/^copy_cli_credential()/,/^}}/p;
  /^prepare_cli_runtime()/,/^}}/p;
  /^prepare_cli_login_shell()/,/^}}/p' '{RUN_AGENT}')"
CLI_CONCURRENT_RUN=1
CLI_RUNTIME_STATE_ROOT='{self.state}/cli-runtimes'
CLI_RUNTIME_LAYOUT=owner
FACTORY_CURSOR_SESSION_HOME='{self.home}'
HOME='{self.home}'
source '{BACKEND_POLICY}'
ADAPTER=cursor-openai
CLI_ATTEMPT_ID=login-shell
CLI_RUNTIME_ROOT=
CLI_PROVIDER_HOME=
CLI_PROVIDER_TMPDIR=
CLI_CURSOR_CONFIG_DIR=
CLI_CURSOR_DATA_DIR=
CLI_CLAUDE_CONFIG_DIR=
CLI_CLAUDE_SETTINGS=
FACTORY_CERTIFIED_NODE_VERSION=v22.22.0
FACTORY_CERTIFIED_NPM_VERSION=10.9.4
TASK_PATH='{task_path}'
TERMINAL_REASON_CODE=
prepare_cli_runtime
prepare_cli_login_shell
printf '%s\\n' "$CLI_PROVIDER_HOME"
"""
        result = subprocess.run(
            ["/bin/bash", "-c", script], text=True, capture_output=True,
            check=True, timeout=30,
        )
        provider_home = Path(result.stdout.strip())
        marker = self.root / "product-command-started"
        environment = {
            "HOME": str(provider_home),
            "LANG": "C",
            "LOGNAME": os.environ.get("LOGNAME", "factory"),
            "MARKER": str(marker),
            "PATH": f"{homebrew}:{task_path}",
            "SHELL": "/bin/zsh",
            "USER": os.environ.get("USER", "factory"),
        }
        command = 'node --version; npm --version; : > "$MARKER"'
        ready = subprocess.run(
            ["/bin/zsh", "-lc", command], env=environment, text=True,
            capture_output=True, check=False, timeout=30,
        )
        self.assertEqual(ready.returncode, 0, ready.stderr)
        self.assertEqual(ready.stdout.splitlines(), ["v22.22.0", "10.9.4"])
        self.assertTrue(marker.is_file())

        for tool, expected, drifted in (
            ("node", "v22.22.0", "v22.22.1"),
            ("npm", "10.9.4", "10.9.5"),
        ):
            with self.subTest(tool=tool):
                marker.unlink(missing_ok=True)
                path = pinned / tool
                path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{drifted}'\n")
                path.chmod(0o755)
                refused = subprocess.run(
                    ["/bin/zsh", "-lc", command], env=environment, text=True,
                    capture_output=True, check=False, timeout=30,
                )
                self.assertEqual(refused.returncode, 126)
                self.assertIn("Factory product runtime mismatch", refused.stderr)
                self.assertFalse(marker.exists())
                path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{expected}'\n")
                path.chmod(0o755)

    def test_cursor_readiness_uses_disposable_home(self) -> None:
        binary_root = self.root / "bin"
        binary_root.mkdir()
        trace = self.root / "cursor-homes"
        agent = binary_root / "agent"
        agent.write_text(
            """#!/bin/bash
set -eu
mkdir -p "$HOME/.cursor"
printf '{"rewritten":true}\\n' > "$HOME/.cursor/cli-config.json"
chmod 644 "$HOME/.cursor/cli-config.json"
printf '%s\\n' "$HOME" >> "$TRACE_FILE"
case "${1:-}" in
  --version) printf '%s\\n' 'agent 2026.07.test' ;;
  --help) printf '%s\\n' '--print --output-format --workspace --model --force --trust' ;;
  status) printf '%s\\n' '{"authenticated":true}' ;;
  models) printf '%s\\n' 'gpt-5.6-sol-high - GPT-5.6 Sol 1M High' ;;
  *) exit 2 ;;
esac
"""
        )
        agent.chmod(0o700)
        environment = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": f"{binary_root}:{os.environ['PATH']}",
            "TRACE_FILE": str(trace),
            "FACTORY_CURSOR_FALLBACK_ENABLED": "1",
            "CURSOR_AGENT_VERSION": "2026.07.test",
            "CURSOR_OPENAI_MODEL": "gpt-5.6-sol-high",
            "FACTORY_CURSOR_ACCOUNT_DB": str(
                self.state / "accounting/probe-must-not-create.sqlite3"
            ),
        }
        command = (
            f"source '{BACKEND_POLICY}'; "
            "factory_probe_adapter cursor-openai; "
            'printf "%s:%s\\n" "$PROBE_STATE" "$PROBE_REASON"'
        )
        result = subprocess.run(
            ["/bin/bash", "-c", command],
            text=True,
            capture_output=True,
            check=True,
            env=environment,
            timeout=30,
        )
        self.assertEqual(result.stdout, "READY:local_contract_ready\n")
        source_config = self.home / ".cursor/cli-config.json"
        self.assertEqual(source_config.read_text(), '{"version":1}\n')
        self.assertEqual(stat.S_IMODE(source_config.stat().st_mode), 0o600)
        self.assertFalse(Path(environment["FACTORY_CURSOR_ACCOUNT_DB"]).exists())
        probe_homes = trace.read_text().splitlines()
        self.assertTrue(probe_homes)
        self.assertEqual(len(set(probe_homes)), 1)
        self.assertNotEqual(probe_homes[0], str(self.home))
        self.assertFalse(Path(probe_homes[0]).exists())

        source_config.chmod(0o644)
        before = trace.read_text()
        refused = subprocess.run(
            ["/bin/bash", "-c", command],
            text=True,
            capture_output=True,
            check=True,
            env=environment,
            timeout=30,
        )
        self.assertEqual(
            refused.stdout, "INVALID:cursor_cli_config_mode_0644\n"
        )
        self.assertEqual(trace.read_text(), before)

        source_config.chmod(0o600)
        source_auth = self.home / ".cursor/auth.json"
        source_auth.chmod(0o644)
        refused = subprocess.run(
            ["/bin/bash", "-c", command],
            text=True,
            capture_output=True,
            check=True,
            env=environment,
            timeout=30,
        )
        self.assertEqual(refused.stdout, "INVALID:cursor_auth_mode_0644\n")

        source_auth.chmod(0o600)
        source_config.unlink()
        refused = subprocess.run(
            ["/bin/bash", "-c", command],
            text=True,
            capture_output=True,
            check=True,
            env=environment,
            timeout=30,
        )
        self.assertEqual(
            refused.stdout, "INVALID:cursor_credential_pair_incomplete\n"
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
            arguments = [
                    sys.executable,
                    str(CLI_RUNTIME),
                    "--coordinator",
                    str(COORDINATOR),
                    "--db",
                    str(self.state / "accounting/state-v2.sqlite3"),
                    "--policy",
                    str(self.state / "provider-policy.json"),
                    "--adapter",
                    adapter,
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
            ]
            if adapter.startswith("cursor-"):
                lease = f"{attempt}-account"
                admission = self.cursor_account("acquire", lease)
                self.assertTrue(admission["admitted"])
                self.assertTrue(
                    self.cursor_account("bind-runtime", lease)["bound"]
                )
                activation = json.loads(
                    (self.state / "isolated-v1.enabled").read_text(
                        encoding="utf-8"
                    )
                )
                arguments.extend([
                    "--account-db",
                    str(self.state / "accounting/cursor-account.sqlite3"),
                    "--account-lease-id", lease,
                    "--account-owner-pid", str(os.getpid()),
                    "--account-owner-pgid", str(os.getpgrp()),
                    "--account-owner-start", self.owner_start,
                    "--account-policy-sha256", activation["policy_sha256"],
                    "--trust-scope", "production-certified",
                ])
            arguments.extend([
                    "--",
                    sys.executable,
                    "-c",
                    command,
                    str(markers / f"ready-{attempt}"),
            ])
            processes[attempt] = subprocess.Popen(
                arguments,
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
            "  printf '{\"input_tokens\":%s,\"cached_input_tokens\":%s,\"output_tokens\":%s}\\n' "
            "\"${STUB_CODEX_INPUT_TOKENS:-10}\" "
            "\"${STUB_CODEX_CACHED_INPUT_TOKENS:-0}\" "
            "\"${STUB_CODEX_OUTPUT_TOKENS:-4}\"\n"
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
        command = [
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
        ]
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            env=environment,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"input_tokens":10', result.stdout)
        cached_command = list(command)
        cached_command[cached_command.index("--budget") + 1] = "10"
        cached_command[cached_command.index("--model") + 1] = "gpt-5.6-terra"
        cached = subprocess.run(
            cached_command,
            text=True,
            capture_output=True,
            check=False,
            env={
                **environment,
                "STUB_CODEX_INPUT_TOKENS": "8098204",
                "STUB_CODEX_CACHED_INPUT_TOKENS": "7935232",
                "STUB_CODEX_OUTPUT_TOKENS": "32434",
            },
            timeout=10,
        )
        self.assertEqual(cached.returncode, 0, cached.stdout + cached.stderr)
        self.assertIn("turns=1 cost_usd=2.7561", cached.stdout)
        over_budget = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            env={
                **environment,
                "STUB_CODEX_INPUT_TOKENS": "1000000",
                "STUB_CODEX_OUTPUT_TOKENS": "1000000",
            },
            timeout=10,
        )
        self.assertEqual(over_budget.returncode, 7)
        self.assertIn(
            "BUDGET EXCEEDED: run cost $11.2500 > per-run budget $1",
            over_budget.stderr,
        )
        self.assertIn("turns=1 cost_usd=11.2500", over_budget.stdout)
        self.cleanup_runtime("codex", "adapter-codex", runtime)
        self.assertFalse(runtime.exists())

    def test_doctor_fails_closed_until_multi_ticket_provider_is_ready(self) -> None:
        product = self.root / "product"
        (product / "factory").mkdir(parents=True)
        sha = "1" * 40
        (product / "factory/KIT_PIN").write_text(sha + "\n")
        (product / "factory/PROJECT.env").write_text("MAX_CONCURRENT_TICKETS=3\n")
        binary_root = self.root / "doctor-bin"
        binary_root.mkdir()
        for name in ("agent", "claude", "codex", "gh", "factory"):
            path = binary_root / name
            path.write_text("#!/bin/sh\nprintf '%s\\n' test\n")
            path.chmod(0o700)
        launcher = self.home / ".factory/bin/factory-launch"
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes((ROOT / "scripts/factory-launch").read_bytes())
        launcher.chmod(0o700)
        label = "com.factory.legacy-relay.relay"
        launch_agents = self.home / "Library/LaunchAgents"
        launch_agents.mkdir(parents=True)
        service = launch_agents / f"{label}.plist"
        with service.open("wb") as stream:
            plistlib.dump({
                "Label": label,
                "ProgramArguments": [str(launcher), "relay", "legacy-relay"],
                "StartInterval": 180,
                "RunAtLoad": True,
                "StandardOutPath": str(product / "factory/legacy-relay.log"),
                "StandardErrorPath": str(product / "factory/legacy-relay.err.log"),
            }, stream)
        launchctl = self.root / "launchctl"
        launchctl.write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            f"print-disabled) printf '%s\\n' 'disabled services = {{' "
            f"'  \"{label}\" => enabled' '}}' ;;\n"
            "print) printf '%s\\n' 'arguments = {' "
            f"'  {launcher}' '  relay' '  legacy-relay' '}}' ;;\n"
            "*) exit 2 ;;\n"
            "esac\n"
        )
        launchctl.chmod(0o700)
        environment = {
            **os.environ,
            "FACTORY_DOCTOR_TEST_LAUNCHCTL": str(launchctl),
            "FACTORY_TEST_MODE": "1",
            "FACTORY_TRUSTED_TEST_HARNESS": "1",
            "FACTORY_PROVIDER_ACTIVATION": str(self.state / "isolated-v1.enabled"),
            "FACTORY_PROVIDER_APPLY_LOCK_ROOT": str(self.state / "provider-apply-locks"),
            "FACTORY_PROVIDER_ATTEMPT_ROOT": str(self.state / "provider-attempts"),
            "FACTORY_PROVIDER_DB": str(self.state / "accounting/state-v2.sqlite3"),
            "FACTORY_PROVIDER_POLICY": str(self.state / "provider-policy.json"),
            "FACTORY_CLI_RUNTIME_ROOT": str(self.root),
            "FACTORY_RELEASE_CONTRACT_VERSION": "2.0.0",
            "HOME": str(self.home),
            "PATH": f"{binary_root}:{os.environ['PATH']}",
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

        runs = product / "factory/runs"
        runs.mkdir()
        (runs / "stale-1.pid").write_text("pid=99999999\n")
        (runs / "stale-1.meta").write_text("ticket=T-1\n")
        stale = subprocess.run(
            arguments,
            text=True,
            capture_output=True,
            check=False,
            env=environment,
            timeout=30,
        )
        self.assertEqual(
            json.loads(stale.stdout)["checks"]["runtime"]["runs"],
            [{
                "recovery_command": None,
                "recovery_reason": "unsupported_stale_run",
                "run_id": "stale-1",
                "state": "stale",
                "ticket": "T-1",
            }],
        )


if __name__ == "__main__":
    unittest.main()
