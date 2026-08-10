#!/usr/bin/env python3
"""Focused sealed qualification-environment test."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import hmac
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock
import json


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "qualification_environment", ROOT / "scripts/qualification-environment.py"
)
assert SPEC and SPEC.loader
ENVIRONMENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ENVIRONMENT)


def run(root: Path, *arguments: str) -> str:
    return subprocess.run(
        arguments, cwd=root, text=True, capture_output=True, check=True,
    ).stdout.strip()


class QualificationEnvironmentTest(unittest.TestCase):
    def setUp(self) -> None:
        if not Path("/private/tmp").is_dir():
            self.skipTest("qualification trust root is macOS-only")
        self.workspace = Path(tempfile.mkdtemp(prefix="qualification-test."))
        self.original_home = os.environ.get("HOME")
        self.original_operator_seed = os.environ.get(
            "FACTORY_QUALIFICATION_OPERATOR_MAP_SEED"
        )
        self.home = self.workspace / "home"
        self.home.mkdir(mode=0o700)
        (self.home / ".factory").mkdir(mode=0o700)
        self.global_env = self.home / ".factory/global.env"
        self.global_env.write_text(
            "CLAUDE_CODE_PINNED=2.1.223\n", encoding="utf-8",
        )
        self.global_env.chmod(0o600)
        os.environ["HOME"] = str(self.home)
        self.root = Path(tempfile.mkdtemp(
            prefix="nysa-sf-qualification.q-", dir="/private/tmp",
        )).resolve()
        os.chmod(self.root, 0o700)
        self.factory = self.workspace / "factory"
        (self.factory / "integrations/hermes/bin").mkdir(parents=True)
        (self.factory / "integrations/hermes/contract.json").write_text(
            '{"contract_version":"1.8.0"}\n', encoding="utf-8",
        )
        launcher = self.factory / "integrations/hermes/bin/factory-launch"
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        launcher.chmod(0o755)
        model_control = self.factory / "scripts/model-control.sh"
        model_control.parent.mkdir(exist_ok=True)
        model_control.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' '{\"checks\":[],\"profile_id\":\"fixture\","
            "\"readiness_sha256\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\","
            "\"schema\":\"nysa.software-factory.qualification-fallback-readiness/v1\","
            "\"status\":\"ready\"}'\n",
            encoding="utf-8",
        )
        model_control.chmod(0o755)
        (self.factory / "scripts/model-routing").mkdir(parents=True)
        shutil.copy2(
            ROOT / "scripts/provider-activation.py",
            self.factory / "scripts/provider-activation.py",
        )
        shutil.copy2(
            ROOT / "scripts/provider-coordinator.py",
            self.factory / "scripts/provider-coordinator.py",
        )
        shutil.copy2(
            ROOT / "scripts/ticket-passport.py",
            self.factory / "scripts/ticket-passport.py",
        )
        shutil.copy2(
            ROOT / "scripts/qualification-reducer.py",
            self.factory / "scripts/qualification-reducer.py",
        )
        linear_sync = self.factory / "scripts/linear-sync.py"
        linear_sync.write_text("""#!/usr/bin/env python3
import argparse, json, os
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument('--factory-root')
parser.add_argument('--ticket')
parser.add_argument('--initialize', action='store_true')
args = parser.parse_args()
mapping_path = Path(os.environ['FACTORY_OPERATOR_MAP'])
mapping = json.loads(mapping_path.read_text())
entry = mapping['tickets'].setdefault(args.ticket, {})
entry.setdefault('issue_id', 'issue-' + args.ticket)
entry.setdefault('identifier', 'SF-' + args.ticket.split('-')[1])
entry['operator_fields_initialized'] = True
entry['operator'] = {
    'observed_at': '2026-08-07T00:00:00+00:00', 'priority': 'none',
}
selected = mapping['_sync'].setdefault('selected_ticket_success_at', {})
selected[args.ticket] = '2026-08-07T00:00:00+00:00'
mapping_path.write_text(json.dumps(mapping, sort_keys=True) + '\\n')
mapping_path.chmod(0o600)
lock = mapping_path.parent / '.linear-sync-cycle.lock'
lock.touch(mode=0o600, exist_ok=True)
lock.chmod(0o600)
ledger = Path(os.environ['FACTORY_LEDGER'])
ledger.write_text('ticket,role,cost_usd,exit_status\\n')
ledger.chmod(0o600)
""", encoding="utf-8")
        linear_sync.chmod(0o755)
        (self.factory / "scripts/lib").mkdir()
        shutil.copy2(
            ROOT / "scripts/certification-preflight.py",
            self.factory / "scripts/certification-preflight.py",
        )
        shutil.copy2(
            ROOT / "scripts/lib/certification_plan.py",
            self.factory / "scripts/lib/certification_plan.py",
        )
        shutil.copy2(
            ROOT / "scripts/lib/role_output.py",
            self.factory / "scripts/lib/role_output.py",
        )
        (self.factory / "scripts/model-routing/catalog-v1.json").write_text(
            json.dumps({
                "routes": [{
                    "account_route_id": "cursor",
                    "adapter": "cursor-openai",
                    "enabled": True,
                    "provider_family": "openai",
                    "route_id": "cursor-test",
                    "selection_id": "test-model",
                }],
            }) + "\n",
            encoding="utf-8",
        )
        shutil.copy2(
            ROOT / "scripts/model-routing/handoff-boundaries-v1.json",
            self.factory / "scripts/model-routing/handoff-boundaries-v1.json",
        )
        shutil.copy2(
            ROOT / "scripts/lib/lane-path-sentinel.py",
            self.factory / "scripts/lib/lane-path-sentinel.py",
        )
        run(self.factory, "git", "init", "-q", "-b", "main")
        run(self.factory, "git", "config", "user.name", "Test")
        run(self.factory, "git", "config", "user.email", "test@example.invalid")
        run(self.factory, "git", "add", ".")
        run(self.factory, "git", "commit", "-qm", "candidate")
        self.sha = run(self.factory, "git", "rev-parse", "HEAD")

        self.product = self.workspace / "product"
        (self.product / "factory").mkdir(parents=True)
        (self.product / "factory/KIT_PIN").write_text(
            self.sha + "\n", encoding="utf-8",
        )
        (self.product / "factory/QUALIFICATION.json").write_text(
            json.dumps({
                "budget_usd": "100.000000",
                "capacity": 3,
                "contract_version": "1.8.0",
                "factory_sha": self.sha,
                "generation": 1,
                "per_run_budget_usd": "2.000000",
                "per_ticket_budget_usd": "25.000000",
                "schema": "nysa.software-factory.qualification/v2",
                "target_done": 3,
                "tickets": ["T-101", "T-102", "T-103"],
            }) + "\n",
            encoding="utf-8",
        )
        (self.product / "factory/PROJECT.env").write_text(
            "PREVIEW_PROVIDER=railway\n", encoding="utf-8",
        )
        (self.product / ".gitignore").write_text(
            "factory/runs/\n", encoding="utf-8",
        )
        (self.product / "factory/tickets").mkdir()
        for ticket in ("T-101", "T-102", "T-103"):
            (self.product / f"factory/tickets/{ticket}.md").write_text(
                f"# {ticket}\n\nState: Ready\nProduct-Decisions: frozen\n"
                "Initiative: I-001\n"
                "Depends-On: none\nFixture-Seams: none\n"
                "Authentication-Seams: none\nProtected-Test-Conflicts: none\n"
                "Builder ownership: app/server.js only\n",
                encoding="utf-8",
            )
        (self.product / "factory/certification-plan.json").write_text(
            json.dumps({
                "phases": [{
                    "artifacts": [],
                    "command": ["true"],
                    "depends_on": [],
                    "name": "control",
                    "network": "denied",
                }],
                "runtime": {
                    "node": run(self.workspace, "node", "--version"),
                    "npm": run(self.workspace, "npm", "--version"),
                },
                "schema": "nysa.software-factory.certification-plan/v2",
            }) + "\n",
            encoding="utf-8",
        )
        run(self.product, "git", "init", "-q", "-b", "main")
        run(self.product, "git", "config", "user.name", "Test")
        run(self.product, "git", "config", "user.email", "test@example.invalid")
        run(self.product, "git", "remote", "add", "origin", "git@example.invalid")
        run(self.product, "git", "add", ".")
        run(self.product, "git", "commit", "-qm", "product")
        run(
            self.product, "git", "update-ref", "refs/remotes/origin/main",
            run(self.product, "git", "rev-parse", "HEAD"),
        )
        self.operator_seed = self.workspace / "linear-map-seed.json"
        ENVIRONMENT.write(self.operator_seed, {
            "_config": {
                "labels": {}, "states": {}, "team_id": "team-id",
                "team_key": "SF", "template_id": "template-id",
            },
            "_sync": {},
            "initiatives": {"I-001": {"project_id": "project-id"}},
            "tickets": {},
        })
        os.environ["FACTORY_QUALIFICATION_OPERATOR_MAP_SEED"] = str(
            self.operator_seed
        )

    def write_passport(
        self, path: Path, secret: bytes, ticket: str, factory_sha: str,
        source_factory_sha: str | None = None,
    ) -> None:
        source = source_factory_sha or factory_sha
        migrated = factory_sha != source
        value = {
            "base_history": ["9" * 40, *(["7" * 40] if migrated else [])],
            "branch": f"ticket/{ticket}",
            "charge_records": [],
            "completed_role_evidence": [],
            "contract_version": "1.8.0",
            "cumulative_charges_micro_usd": 0,
            "current_stage": "RUN planner",
            "current_state": "Planning",
            "factory_release_history": [
                {"contract_version": "1.8.0", "factory_sha": source},
                *([{"contract_version": "1.8.0", "factory_sha": factory_sha}]
                  if migrated else []),
            ],
            "factory_sha": factory_sha,
            "head_sha": "6" * 40 if migrated else "1" * 40,
            "head_tree": "a" * 40,
            "migration_history": ([{
                "from_factory_sha": source,
                "from_head_sha": "1" * 40,
                "from_passport_file_sha256": "2" * 64,
                "from_passport_sha256": "3" * 64,
                "from_protected_base_sha": "9" * 40,
                "from_route_plan_sha256": "5" * 64,
                "schema": "nysa.software-factory.ticket-passport-migration/v2",
                "to_factory_sha": factory_sha,
                "to_head_sha": "6" * 40,
                "to_protected_base_sha": "7" * 40,
                "to_route_plan_sha256": "8" * 64,
            }] if migrated else []),
            "nonce": "1" * 32,
            "parent_digest": "3" * 64 if migrated else None,
            "parent_file_sha256": "2" * 64 if migrated else None,
            "product_origin_sha256": "a" * 64,
            "project": "relay",
            "protected_base_sha": "7" * 40 if migrated else "9" * 40,
            "publication_state": "none",
            "route_plan_sha256": "8" * 64 if migrated else "5" * 64,
            "schema": "nysa.software-factory.ticket-passport/v1",
            "ticket": ticket,
            "ticket_blob": "b" * 40,
            "transition_receipt_sha256": "c" * 64,
        }
        self.sign_passport(path, secret, value)

    def sign_passport(
        self, path: Path, secret: bytes, value: dict[str, object],
    ) -> None:
        value["authentication_sha256"] = hmac.new(
            secret, ENVIRONMENT.canonical(value), hashlib.sha256,
        ).hexdigest()
        value["passport_sha256"] = hashlib.sha256(
            ENVIRONMENT.canonical(value)
        ).hexdigest()
        path.write_bytes(ENVIRONMENT.canonical(value))
        path.chmod(0o600)

    def tearDown(self) -> None:
        for base, directories, files in os.walk(self.root, topdown=False):
            for name in files:
                (Path(base) / name).chmod(0o600)
            for name in directories:
                (Path(base) / name).chmod(0o700)
        shutil.rmtree(self.root)
        if self.original_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.original_home
        if self.original_operator_seed is None:
            os.environ.pop("FACTORY_QUALIFICATION_OPERATOR_MAP_SEED", None)
        else:
            os.environ["FACTORY_QUALIFICATION_OPERATOR_MAP_SEED"] = (
                self.original_operator_seed
            )
        shutil.rmtree(self.workspace)

    def test_prepares_exact_read_only_candidate_once(self) -> None:
        args = argparse.Namespace(
            factory_root=self.factory,
            product_root=self.product,
            project="relay",
            root=self.root,
        )
        value = ENVIRONMENT.prepare(args)
        release = Path(value["launcher"]).parents[3]
        authority = Path(value["authority_root"])
        self.assertEqual(value["factory_sha"], self.sha)
        lane = ENVIRONMENT.qualification_lane(self.root, "relay")
        self.assertEqual(lane["active"]["kit_sha"], self.sha)
        active = ENVIRONMENT.read(self.root / "projects/relay/active.json")
        receipt = ENVIRONMENT.read(
            self.root / "receipts" / f"{active['receipt_id']}.json"
        )
        operator_map = authority / "operator/linear-map.json"
        runtime_ledger = authority / "operator/runtime-ledger.csv"
        self.assertEqual(active["operator_map_path"], str(operator_map))
        self.assertEqual(active["runtime_ledger_path"], str(runtime_ledger))
        self.assertEqual(receipt["operator_map_path"], str(operator_map))
        self.assertEqual(receipt["runtime_ledger_path"], str(runtime_ledger))
        self.assertEqual(receipt["fallback_readiness_sha256"], "a" * 64)
        self.assertEqual(active["fallback_readiness_sha256"], "a" * 64)
        self.assertEqual(
            set(ENVIRONMENT.read(operator_map)["tickets"]),
            {"T-101", "T-102", "T-103"},
        )
        self.assertTrue(runtime_ledger.is_file())
        self.assertEqual(run(self.product, "git", "status", "--porcelain"), "")
        for relative in (
            "linear-map.json", ".linear-sync-cycle.lock",
            ".linear-sync.lock", ".linear-operator-clears", "runtime-ledger.csv",
        ):
            self.assertFalse((self.product / "factory" / relative).exists())
        runs = self.product / "factory/runs"
        self.assertTrue(runs.is_dir())
        self.assertEqual(runs.stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            value["product_sha"], run(self.product, "git", "rev-parse", "HEAD")
        )
        self.assertEqual(value["runtime_tuple"]["factory_sha"], self.sha)
        self.assertEqual(ENVIRONMENT.git_tree(release), value["factory_tree"])
        self.assertFalse(release.stat().st_mode & 0o222)
        self.assertEqual(
            (self.root / "profile/projects/relay.env").read_text(),
            f"PRODUCT_ROOT={self.product.resolve()}\n",
        )
        self.assertEqual(
            (self.root / "global.env").read_bytes(),
            self.global_env.read_bytes(),
        )
        self.assertEqual((self.root / "global.env").stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            json.loads((self.root / "marker.json").read_text()),
            {
                "mode": "qualification",
                "schema": "nysa.software-factory.qualification-environment/v1",
            },
        )
        status = json.loads(run(
            self.root,
            "/usr/bin/python3",
            str(release / "scripts/provider-activation.py"),
            "--config", str(authority / "provider/provider-activation.json"),
            "--policy", str(authority / "provider/provider-policy.json"),
            "--contract-version", "1.8.0",
            "--status",
        ))
        self.assertEqual(status["execution_mode"], "cli-concurrent-v1")
        policy = json.loads(
            (authority / "provider/provider-policy.json").read_text()
        )
        self.assertEqual(policy["coupled_max_concurrent"], 3)
        self.assertEqual(policy["global"]["max_concurrent"], 3)
        launcher_text = (
            ROOT / "integrations/hermes/bin/factory-launch"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'PROVIDER_STATE_ROOT="$ACTIVE_PROVIDER_STATE"', launcher_text
        )
        self.assertIn(
            'CONTROLLER_STATE_DIR="$ACTIVE_CONTROLLER_STATE"', launcher_text
        )
        self.assertIn(
            '--state-dir "$CONTROLLER_STATE_DIR" --project "$PROJECT"',
            launcher_text,
        )
        self.assertIn(
            'exec /usr/bin/env -i "HOME=$HOME" "PATH=$SAFE_PATH" "TMPDIR=$SAFE_TMPDIR"',
            launcher_text,
        )
        self.assertIn(
            '"FACTORY_CLI_LANE_ROOT=$QUALIFICATION_ROOT"',
            launcher_text,
        )
        self.assertIn(
            '"FACTORY_PROVIDER_PRODUCT_ID=$PROJECT:$KIT_SHA"',
            launcher_text,
        )
        runner_text = (ROOT / "scripts/run-agent.sh").read_text(encoding="utf-8")
        self.assertIn(
            '"$FACTORY_PROVIDER_PRODUCT_ID" != "$TRANSITION_PROJECT:$FACTORY_KIT_SHA"',
            runner_text,
        )
        self.assertIn(
            '"${FACTORY_CLI_LANE_ROOT:-}" != /*', runner_text,
        )
        self.assertNotIn(
            '[[ -z "$DEVELOPMENT_LANE_ROOT" ||', runner_text,
        )
        self.assertIn('CLI_PRODUCT_ID="$PROVIDER_PRODUCT_ID"', runner_text)
        self.assertIn('ISOLATED_PRODUCT_ID="$PROVIDER_PRODUCT_ID"', runner_text)
        self.assertIn(
            '"FACTORY_QUALIFICATION_PRODUCT_SHA=$ACTIVE_PRODUCT_SHA"',
            launcher_text,
        )
        self.assertIn(
            '"FACTORY_QUALIFICATION_PRODUCT_TREE=$ACTIVE_PRODUCT_TREE"',
            launcher_text,
        )
        self.assertIn(
            '"FACTORY_QUALIFICATION_FALLBACK_READINESS_SHA256=$ACTIVE_FALLBACK_READINESS_SHA256"',
            launcher_text,
        )
        self.assertIn(
            '"FACTORY_QUALIFICATION_MANIFEST=$PRODUCT_ROOT/factory/QUALIFICATION.json"',
            launcher_text,
        )
        self.assertIn(
            '"FACTORY_CLI_RUNTIME_ROOT=$CLI_RUNTIME_ROOT"',
            launcher_text,
        )
        self.assertIn('CLI_RUNTIME_ROOT="$QUALIFICATION_ROOT"', launcher_text)
        self.assertIn(
            'GLOBAL_ENV_PATH="$QUALIFICATION_ROOT/global.env"', launcher_text,
        )
        self.assertIn('"FACTORY_GLOBAL_ENV=$GLOBAL_ENV_PATH"', launcher_text)
        self.assertIn(
            '--cli-root "${FACTORY_CLI_RUNTIME_ROOT:-}"', runner_text,
        )
        for relative in (
            "provider/accounting",
            "provider/cli-runtimes",
            "provider/provider-apply-locks",
            "provider/provider-attempts",
        ):
            path = authority / relative
            self.assertTrue(path.is_dir())
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        configuration_lock = authority / "provider/provider-configuration.lock"
        self.assertTrue(configuration_lock.is_file())
        self.assertEqual(configuration_lock.stat().st_mode & 0o777, 0o600)
        with (
            mock.patch.object(
                ENVIRONMENT, "initialize_selected_linear",
                side_effect=AssertionError("complete replay must not call Linear"),
            ),
            mock.patch.object(
                ENVIRONMENT, "qualification_fallback_readiness",
                wraps=ENVIRONMENT.qualification_fallback_readiness,
            ) as readiness,
        ):
            replay = ENVIRONMENT.prepare(args)
        self.assertEqual(replay, value)
        readiness.assert_called_once()

    def test_prepare_recovers_each_exact_crash_prefix_and_response_loss(self) -> None:
        args = argparse.Namespace(
            factory_root=self.factory, product_root=self.product,
            project="relay", root=self.root,
        )
        original_json = ENVIRONMENT.write_exact
        original_bytes = ENVIRONMENT.write_bytes_exact
        authority = self.home / ".factory/qualification/relay"
        self.assertEqual(
            ENVIRONMENT.preparation_state(self.root, authority), "fresh",
        )

        def crash_global(path, raw):
            original_bytes(path, raw)
            if path.name == "global.env":
                raise ENVIRONMENT.EnvironmentError("simulated response loss")

        with (
            mock.patch.object(
                ENVIRONMENT, "write_bytes_exact", side_effect=crash_global,
            ),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "simulated response loss",
            ),
        ):
            ENVIRONMENT.prepare(args)
        self.assertTrue((self.root / "global.env").is_file())
        self.assertFalse((self.root / "marker.json").exists())

        def crash_json(predicate):
            crashed = False

            def interrupted(path, value):
                nonlocal crashed
                original_json(path, value)
                if not crashed and predicate(path):
                    crashed = True
                    raise ENVIRONMENT.EnvironmentError("simulated response loss")

            return interrupted

        predicates = (
            lambda path: path.name == "provider-activation.json",
            lambda path: path.parent.name == "receipts",
            lambda path: path.name == "authority.json",
            lambda path: path.name == "active.json",
            lambda path: path.name == "environment.json",
        )
        for index, predicate in enumerate(predicates):
            with (
                mock.patch.object(
                    ENVIRONMENT, "write_exact", side_effect=crash_json(predicate),
                ),
                self.assertRaisesRegex(
                    ENVIRONMENT.EnvironmentError, "simulated response loss",
                ),
            ):
                ENVIRONMENT.prepare(args)
            if index == 0:
                self.assertEqual(
                    ENVIRONMENT.preparation_state(self.root, authority),
                    "exact-incomplete",
                )

        crashed = False

        def crash_registry(path, raw):
            nonlocal crashed
            original_bytes(path, raw)
            if not crashed and path.name == "relay.env":
                crashed = True
                raise ENVIRONMENT.EnvironmentError("simulated response loss")

        with (
            mock.patch.object(
                ENVIRONMENT, "write_bytes_exact", side_effect=crash_registry,
            ),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "simulated response loss",
            ),
        ):
            ENVIRONMENT.prepare(args)
        value = ENVIRONMENT.prepare(args)
        self.assertEqual(value["status"], "prepared")
        self.assertEqual(
            ENVIRONMENT.preparation_state(
                self.root, Path(value["authority_root"]),
            ),
            "exact-complete",
        )

    def test_prepare_serializes_same_project_and_replays_exact_result(self) -> None:
        args = argparse.Namespace(
            factory_root=self.factory, product_root=self.product,
            project="relay", root=self.root,
        )
        original = ENVIRONMENT.qualification_fallback_readiness
        guard = threading.Lock()
        active = 0
        maximum = 0

        def slow_readiness(*arguments):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.05)
            try:
                return original(*arguments)
            finally:
                with guard:
                    active -= 1

        with (
            mock.patch.object(
                ENVIRONMENT, "qualification_fallback_readiness",
                side_effect=slow_readiness,
            ),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            results = list(pool.map(lambda _: ENVIRONMENT.prepare(args), range(2)))
        self.assertEqual(maximum, 1)
        self.assertEqual(results[0], results[1])

    def test_prepare_refuses_torn_release_without_deleting_it(self) -> None:
        args = argparse.Namespace(
            factory_root=self.factory, product_root=self.product,
            project="relay", root=self.root,
        )
        with (
            mock.patch.object(os, "rename", side_effect=OSError("simulated crash")),
            self.assertRaisesRegex(OSError, "simulated crash"),
        ):
            ENVIRONMENT.prepare(args)
        partial = self.root / f"releases/.{self.sha}.partial"
        self.assertTrue(partial.is_dir())
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "partial qualification release",
        ):
            ENVIRONMENT.prepare(args)
        self.assertTrue(partial.is_dir())

    def test_prepare_refuses_missing_root_predecessor_and_changed_snapshot(self):
        args = argparse.Namespace(
            factory_root=self.factory, product_root=self.product,
            project="relay", root=self.root,
        )
        with (
            mock.patch.object(
                ENVIRONMENT, "ensure_release",
                side_effect=ENVIRONMENT.EnvironmentError("simulated interruption"),
            ),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "simulated interruption",
            ),
        ):
            ENVIRONMENT.prepare(args)

        controller = self.home / ".factory/qualification/relay/controller"
        controller.rmdir()
        with (
            mock.patch.object(
                ENVIRONMENT, "initialize_selected_linear",
                side_effect=AssertionError("refusal must precede Linear"),
            ),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "preparation state is torn",
            ),
        ):
            ENVIRONMENT.prepare(args)
        self.assertFalse(controller.exists())
        controller.mkdir(mode=0o700)

        active = controller / "active.json"
        ENVIRONMENT.write(active, {"status": "running"})
        with (
            mock.patch.object(
                ENVIRONMENT, "initialize_selected_linear",
                side_effect=AssertionError("refusal must precede Linear"),
            ),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "controller is active",
            ),
        ):
            ENVIRONMENT.prepare(args)
        self.assertTrue(active.is_file())
        active.unlink()

        missing = self.root / "profile/projects"
        missing.rmdir()
        with (
            mock.patch.object(
                ENVIRONMENT, "initialize_selected_linear",
                side_effect=AssertionError("refusal must precede Linear"),
            ),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "preparation state is torn",
            ),
        ):
            ENVIRONMENT.prepare(args)
        self.assertFalse(missing.exists())

        missing.mkdir(mode=0o700)
        snapshot = self.root / "global.env"
        snapshot.write_bytes(b"CHANGED=true\n")
        snapshot.chmod(0o600)
        with (
            mock.patch.object(
                ENVIRONMENT, "initialize_selected_linear",
                side_effect=AssertionError("refusal must precede Linear"),
            ),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "preparation artifact changed",
            ),
        ):
            ENVIRONMENT.prepare(args)
        self.assertEqual(snapshot.read_bytes(), b"CHANGED=true\n")

    def test_prepare_refuses_provider_gap_before_linear_or_repair(self) -> None:
        args = argparse.Namespace(
            factory_root=self.factory, product_root=self.product,
            project="relay", root=self.root,
        )
        original = ENVIRONMENT.write_exact

        def interrupt(path, value):
            original(path, value)
            if path.name == "provider-activation.json":
                raise ENVIRONMENT.EnvironmentError("simulated interruption")

        with (
            mock.patch.object(ENVIRONMENT, "write_exact", side_effect=interrupt),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "simulated interruption",
            ),
        ):
            ENVIRONMENT.prepare(args)
        provider = self.home / ".factory/qualification/relay/provider"
        policy = provider / "provider-policy.json"
        policy.unlink()
        with (
            mock.patch.object(
                ENVIRONMENT, "initialize_selected_linear",
                side_effect=AssertionError("refusal must precede Linear"),
            ),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "preparation state is torn",
            ),
        ):
            ENVIRONMENT.prepare(args)
        self.assertFalse(policy.exists())
        self.assertTrue((provider / "provider-activation.json").is_file())

    def test_prepare_refuses_mismatch_and_active_controller_without_mutation(self):
        args = argparse.Namespace(
            factory_root=self.factory, product_root=self.product,
            project="relay", root=self.root,
        )
        value = ENVIRONMENT.prepare(args)
        authority = Path(value["authority_root"])
        environment = self.root / "environment.json"
        original_environment = ENVIRONMENT.read(environment)
        changed = dict(original_environment)
        changed["historical_pr_objects"] = ["unexpected"]
        ENVIRONMENT.replace(environment, changed)
        before = environment.read_bytes()
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "preparation artifact changed",
        ):
            ENVIRONMENT.prepare(args)
        self.assertEqual(environment.read_bytes(), before)
        ENVIRONMENT.replace(environment, original_environment)

        noncanonical = json.dumps(original_environment, indent=2).encode() + b"\n"
        environment.write_bytes(noncanonical)
        environment.chmod(0o600)
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "preparation artifact changed",
        ):
            ENVIRONMENT.prepare(args)
        self.assertEqual(environment.read_bytes(), noncanonical)
        ENVIRONMENT.replace(environment, original_environment)

        activation = authority / "provider/provider-activation.json"
        original_activation = ENVIRONMENT.read(activation)
        changed_activation = dict(original_activation)
        changed_activation["enabled"] = False
        ENVIRONMENT.replace(activation, changed_activation)
        before = activation.read_bytes()
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "preparation artifact changed",
        ):
            ENVIRONMENT.prepare(args)
        self.assertEqual(activation.read_bytes(), before)
        ENVIRONMENT.replace(activation, original_activation)

        active_record = ENVIRONMENT.read(
            self.root / "projects/relay/active.json"
        )
        receipt = self.root / f"receipts/{active_record['receipt_id']}.json"
        receipt_value = ENVIRONMENT.read(receipt)
        receipt.unlink()
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "preparation state is torn",
        ):
            ENVIRONMENT.prepare(args)
        self.assertFalse(receipt.exists())
        self.assertTrue(environment.is_file())
        ENVIRONMENT.write(receipt, receipt_value)

        active = authority / "controller/unexpected.json"
        ENVIRONMENT.write(active, {"status": "running"})
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "controller is active",
        ):
            ENVIRONMENT.prepare(args)
        self.assertTrue(active.is_file())

    def test_lane_refuses_digest_valid_foreign_operator_paths_without_mutation(self):
        args = argparse.Namespace(
            factory_root=self.factory,
            product_root=self.product,
            project="relay",
            root=self.root,
        )
        value = ENVIRONMENT.prepare(args)
        authority = Path(value["authority_root"])
        active_path = self.root / "projects/relay/active.json"
        active = ENVIRONMENT.read(active_path)
        receipt = ENVIRONMENT.read(
            self.root / "receipts" / f"{active['receipt_id']}.json"
        )
        foreign = self.home / ".factory/qualification/foreign/operator"
        foreign.mkdir(parents=True, mode=0o700)
        foreign_map = foreign / "linear-map.json"
        foreign_ledger = foreign / "runtime-ledger.csv"
        shutil.copyfile(authority / "operator/linear-map.json", foreign_map)
        shutil.copyfile(authority / "operator/runtime-ledger.csv", foreign_ledger)
        foreign_map.chmod(0o600)
        foreign_ledger.chmod(0o600)
        for value_to_change in (active, receipt):
            value_to_change["operator_map_path"] = str(foreign_map)
            value_to_change["runtime_ledger_path"] = str(foreign_ledger)
        receipt.pop("receipt_id")
        receipt_id = hashlib.sha256(ENVIRONMENT.canonical(receipt)).hexdigest()
        receipt["receipt_id"] = receipt_id
        ENVIRONMENT.write(self.root / f"receipts/{receipt_id}.json", receipt)
        active["receipt_id"] = receipt_id
        ENVIRONMENT.replace(active_path, active)
        journal = authority / "controller/preprovider-handoff.json"
        claims = authority / "controller/claims"
        before_claims = sorted(path.name for path in claims.glob("T-*.json"))
        before_worktrees = sorted(
            path.name for path in (self.root / "worktrees").glob("*")
        )

        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "operator authority path changed"
        ):
            ENVIRONMENT.qualification_lane(self.root, "relay")

        self.assertFalse(journal.exists())
        self.assertEqual(
            sorted(path.name for path in claims.glob("T-*.json")), before_claims,
        )
        self.assertEqual(
            sorted(path.name for path in (self.root / "worktrees").glob("*")),
            before_worktrees,
        )
        self.assertEqual(run(self.product, "git", "status", "--porcelain"), "")

    def test_operator_seed_fails_closed_when_absent_unsafe_or_malformed(self) -> None:
        os.environ.pop("FACTORY_QUALIFICATION_OPERATOR_MAP_SEED")
        args = argparse.Namespace(
            factory_root=self.factory, product_root=self.product,
            project="relay", root=self.root,
        )
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "operator map seed is required",
        ):
            ENVIRONMENT.prepare(args)
        self.assertFalse(self.home.joinpath(".factory/qualification/relay").exists())
        self.assertFalse((self.root / "marker.json").exists())

        unsafe = self.workspace / "unsafe-map.json"
        unsafe.write_bytes(self.operator_seed.read_bytes())
        unsafe.chmod(0o644)
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "unsafe",
        ):
            ENVIRONMENT.prepare(argparse.Namespace(
                **vars(args), operator_map_seed=unsafe,
            ))

        symlink = self.workspace / "linked-map.json"
        symlink.symlink_to(self.operator_seed)
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "unsafe",
        ):
            ENVIRONMENT.prepare(argparse.Namespace(
                **vars(args), operator_map_seed=symlink,
            ))

        malformed = self.workspace / "malformed-map.json"
        ENVIRONMENT.write(malformed, {"tickets": {}})
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "Linear map is malformed",
        ):
            ENVIRONMENT.prepare(argparse.Namespace(
                **vars(args), operator_map_seed=malformed,
            ))

        secret = self.workspace / "secret-map.json"
        value = ENVIRONMENT.read(self.operator_seed)
        value["_config"]["api_token"] = "do-not-copy"
        ENVIRONMENT.write(secret, value)
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "contains secret material",
        ):
            ENVIRONMENT.prepare(argparse.Namespace(
                **vars(args), operator_map_seed=secret,
            ))

        alternate = self.workspace / "alternate-map.json"
        ENVIRONMENT.write(alternate, ENVIRONMENT.read(self.operator_seed))
        os.environ["FACTORY_QUALIFICATION_OPERATOR_MAP_SEED"] = str(
            self.operator_seed
        )
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "operator map seed is ambiguous",
        ):
            ENVIRONMENT.prepare(argparse.Namespace(
                **vars(args), operator_map_seed=alternate,
            ))
        self.assertFalse((self.root / "marker.json").exists())

    def test_partial_selected_initialization_restarts_without_duplication(self) -> None:
        args = argparse.Namespace(
            factory_root=self.factory, product_root=self.product,
            project="relay", root=self.root,
        )
        authority = self.home / ".factory/qualification/relay"

        def interrupt(_factory, _product, map_path, _ledger_path):
            mapping = ENVIRONMENT.read(map_path)
            mapping["tickets"]["T-101"] = {
                "identifier": "SF-101", "issue_id": "issue-T-101",
            }
            mapping["_sync"]["selected_ticket_success_at"] = {
                "T-101": "2026-08-07T00:00:00+00:00",
            }
            ENVIRONMENT.replace(map_path, mapping)
            raise ENVIRONMENT.EnvironmentError("T-102: simulated interruption")

        with (
            mock.patch.object(
                ENVIRONMENT, "initialize_selected_linear", side_effect=interrupt,
            ),
            mock.patch.object(ENVIRONMENT, "prepare_provider") as provider,
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "simulated interruption",
            ),
        ):
            ENVIRONMENT.prepare(args)
        provider.assert_not_called()
        self.assertFalse((self.root / "marker.json").exists())
        self.assertTrue((authority / "operator-bootstrap.json").is_file())
        self.assertEqual(
            ENVIRONMENT.read(authority / "operator/linear-map.json")["tickets"]
            ["T-101"]["issue_id"],
            "issue-T-101",
        )

        self.operator_seed.unlink()
        value = ENVIRONMENT.prepare(args)
        mapping = ENVIRONMENT.read(authority / "operator/linear-map.json")
        self.assertEqual(value["status"], "prepared")
        self.assertEqual(mapping["tickets"]["T-101"]["issue_id"], "issue-T-101")
        self.assertEqual(len(mapping["tickets"]), 3)
        self.assertEqual(run(self.product, "git", "status", "--porcelain"), "")

    def test_partial_bootstrap_ignores_later_seed_change(self) -> None:
        args = argparse.Namespace(
            factory_root=self.factory, product_root=self.product,
            project="relay", root=self.root,
        )

        def interrupt(_factory, _product, map_path, _ledger_path):
            mapping = ENVIRONMENT.read(map_path)
            mapping["tickets"]["T-101"] = {
                "identifier": "SF-101", "issue_id": "issue-T-101",
            }
            mapping["_sync"]["selected_ticket_success_at"] = {
                "T-101": "2026-08-07T00:00:00+00:00",
            }
            ENVIRONMENT.replace(map_path, mapping)
            raise ENVIRONMENT.EnvironmentError("simulated interruption")

        with (
            mock.patch.object(
                ENVIRONMENT, "initialize_selected_linear", side_effect=interrupt,
            ),
            self.assertRaisesRegex(ENVIRONMENT.EnvironmentError, "interruption"),
        ):
            ENVIRONMENT.prepare(args)
        changed = ENVIRONMENT.read(self.operator_seed)
        changed["_sync"]["last_success_at"] = "2026-08-07T01:00:00+00:00"
        ENVIRONMENT.replace(self.operator_seed, changed)

        ENVIRONMENT.prepare(args)
        lane_map = ENVIRONMENT.read(
            self.home / ".factory/qualification/relay/operator/linear-map.json"
        )
        self.assertNotIn("last_success_at", lane_map["_sync"])
        self.assertEqual(lane_map["tickets"]["T-101"]["issue_id"], "issue-T-101")

    def test_second_operator_cycle_remains_outside_product(self) -> None:
        value = ENVIRONMENT.prepare(argparse.Namespace(
            factory_root=self.factory, product_root=self.product,
            project="relay", root=self.root,
        ))
        operator = Path(value["authority_root"]) / "operator"
        mapping = ENVIRONMENT.read(operator / "linear-map.json")
        mapping["_sync"]["last_success_at"] = "2026-08-07T00:01:00+00:00"
        ENVIRONMENT.replace(operator / "linear-map.json", mapping)
        clears = operator / ".linear-operator-clears"
        clears.mkdir(mode=0o700)
        ENVIRONMENT.write(clears / "T-101.json", {"ticket": "T-101"})
        self.assertTrue((operator / ".linear-sync-cycle.lock").is_file())
        self.assertEqual(run(self.product, "git", "status", "--porcelain"), "")

    def test_rejects_unsafe_runtime_root_and_noncanonical_contracts(self) -> None:
        runs = self.product / "factory/runs"
        runs.symlink_to(self.workspace)
        with self.assertRaisesRegex(ENVIRONMENT.EnvironmentError, "factory/runs is unsafe"):
            ENVIRONMENT.prepare(argparse.Namespace(
                factory_root=self.factory, product_root=self.product,
                project="relay", root=self.root,
            ))
        runs.unlink()
        ticket = self.product / "factory/tickets/T-101.md"
        ticket.write_text(ticket.read_text().replace(
            "Product-Decisions: frozen",
            "Product-Decisions: frozen - inherited",
        ))
        run(self.product, "git", "add", "factory/tickets/T-101.md")
        run(self.product, "git", "commit", "-qm", "decorate control field")
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError,
            "Product-Decisions must be exactly frozen",
        ):
            ENVIRONMENT.prepare(argparse.Namespace(
                factory_root=self.factory, product_root=self.product,
                project="relay", root=self.root,
            ))

    def test_rejects_unsafe_global_config_sources(self) -> None:
        unsafe = self.workspace / "unsafe-global.env"
        unsafe.write_text("CLAUDE_CODE_PINNED=2.1.223\n", encoding="utf-8")
        unsafe.chmod(0o644)
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "global config is unsafe",
        ):
            ENVIRONMENT.snapshot_global_config(
                argparse.Namespace(global_env=unsafe), self.root,
            )
        unsafe.chmod(0o600)
        link = self.workspace / "linked-global.env"
        link.symlink_to(unsafe)
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "global config is unsafe",
        ):
            ENVIRONMENT.snapshot_global_config(
                argparse.Namespace(global_env=link), self.root,
            )

    def test_rejects_internal_qualification_dependency(self) -> None:
        ticket = self.product / "factory/tickets/T-103.md"
        ticket.write_text(ticket.read_text().replace("Depends-On: none", "Depends-On: T-101"))
        run(self.product, "git", "add", "factory/tickets/T-103.md")
        run(self.product, "git", "commit", "-qm", "dependent cohort")
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "qualification cohort dependency T-103 -> T-101",
        ):
            ENVIRONMENT.prepare(argparse.Namespace(
                factory_root=self.factory, product_root=self.product,
                project="relay", root=self.root,
            ))

    def test_selected_ticket_authoring_fields_fail_before_lane_creation(self) -> None:
        ticket = self.product / "factory/tickets/T-101.md"
        original = ticket.read_text()
        ticket.write_text(original.replace(
            "Depends-On: none", "Depends-On: none — rationale",
        ))
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "Depends-On is invalid",
        ):
            ENVIRONMENT.validate_selected_contracts(self.product)

        ticket.write_text(original.replace(
            "Fixture-Seams: none", "Fixture-Seams: missing.test.ts",
        ))
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "READINESS BLOCKED.*missing.test.ts",
        ):
            ENVIRONMENT.validate_selected_contracts(self.product)

        ticket.write_text(original)
        project = self.product / "factory/PROJECT.env"
        project.write_text("PREVIEW_PROVIDER=none\nNONVISUAL_PATHS=docs/\n")
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "T-101: preview_capability_missing",
        ):
            ENVIRONMENT.validate_selected_contracts(self.product)

        ticket.write_text(original.replace(
            "Builder ownership: app/server.js only",
            "Builder ownership: generated files only",
        ))
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "Builder ownership",
        ):
            ENVIRONMENT.validate_selected_contracts(self.product)

    def test_qualification_manifest_validation_is_strict(self) -> None:
        path = self.product / "factory/QUALIFICATION.json"
        original = json.loads(path.read_text())
        cases = {
            "unexpected": lambda value: value.update(unexpected=True),
            "contract": lambda value: value.update(contract_version="1.7.0"),
            "capacity": lambda value: value.update(capacity=2),
            "budget": lambda value: value.update(budget_usd="101.000000"),
            "duplicate": lambda value: value.update(
                tickets=["T-101", "T-101", "T-103"]
            ),
            "count": lambda value: value.update(tickets=["T-101", "T-102"]),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                value = dict(original)
                mutate(value)
                path.write_text(json.dumps(value) + "\n")
                with self.assertRaisesRegex(
                    ENVIRONMENT.EnvironmentError,
                    "Contract 1.8 qualification manifest is invalid",
                ):
                    ENVIRONMENT.qualification_manifest(self.product, self.sha)
        path.write_text(json.dumps(original) + "\n")

    def test_selected_linear_refreshes_already_initialized_cohort(self) -> None:
        mapping = self.workspace / "selected-linear-map.json"
        ENVIRONMENT.write(mapping, {
            "_config": {},
            "_sync": {"selected_ticket_success_at": {
                ticket: "2026-08-07T00:00:00+00:00"
                for ticket in ("T-101", "T-102", "T-103")
            }},
            "initiatives": {},
            "tickets": {
                ticket: {
                    "operator_fields_initialized": True,
                    "issue_id": ticket,
                    "operator": {
                        "observed_at": "2026-08-07T00:00:00+00:00",
                        "priority": "none",
                    },
                }
                for ticket in ("T-101", "T-102", "T-103")
            },
        })
        ledger = self.workspace / "selected-runtime-ledger.csv"
        completed = subprocess.CompletedProcess([], 0, "", "")
        def refresh(*_args, **_kwargs):
            value = ENVIRONMENT.read(mapping)
            ticket = _args[0][-2]
            value["_sync"].setdefault("selected_ticket_success_at", {})[ticket] = (
                "2026-08-07T00:00:00+00:00"
            )
            ENVIRONMENT.replace(mapping, value)
            return completed
        with mock.patch.object(
            ENVIRONMENT.subprocess, "run", side_effect=refresh,
        ) as invoked:
            ENVIRONMENT.initialize_selected_linear(
                self.factory, self.product, mapping, ledger, refresh=True,
            )
        self.assertEqual(invoked.call_count, 3)
        self.assertEqual(
            [call.args[0][-2:] for call in invoked.call_args_list],
            [["T-101", "--initialize"], ["T-102", "--initialize"],
             ["T-103", "--initialize"]],
        )
        for call in invoked.call_args_list:
            self.assertEqual(call.kwargs["env"]["FACTORY_OPERATOR_MAP"], str(mapping))
            self.assertEqual(call.kwargs["env"]["FACTORY_LEDGER"], str(ledger))

    def test_rejects_ticket_blob_that_dispatch_would_not_use(self) -> None:
        ticket = self.product / "factory/tickets/T-101.md"
        ticket.write_text(ticket.read_text() + "\n## Log\n\nControl-only edit.\n")
        run(self.product, "git", "add", str(ticket))
        run(self.product, "git", "commit", "-qm", "diverge qualification ticket")

        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError,
            "T-101: qualification ticket source differs from protected dispatch",
        ):
            ENVIRONMENT.prepare(argparse.Namespace(
                factory_root=self.factory, product_root=self.product,
                project="relay", root=self.root,
            ))
        self.assertFalse((self.root / "marker.json").exists())

    def test_rejects_root_too_long_for_cursor_scratch(self) -> None:
        root = Path(tempfile.mkdtemp(
            prefix="nysa-sf-qualification.too-long-", dir="/private/tmp",
        )).resolve()
        os.chmod(root, 0o700)
        try:
            with self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "too long for isolated Cursor",
            ):
                ENVIRONMENT.prepare(argparse.Namespace(
                    factory_root=self.factory,
                    product_root=self.product,
                    project="relay",
                    root=root,
                ))
        finally:
            shutil.rmtree(root)

    def test_runtime_mismatch_fails_before_qualification_materialization(self) -> None:
        plan = self.product / "factory/certification-plan.json"
        value = json.loads(plan.read_text(encoding="utf-8"))
        value["runtime"]["node"] = "v99.0.0"
        plan.write_text(json.dumps(value) + "\n", encoding="utf-8")
        run(self.product, "git", "add", "factory/certification-plan.json")
        run(self.product, "git", "commit", "-qm", "mismatched runtime")
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "runtime_tuple_mismatch",
        ):
            ENVIRONMENT.prepare(argparse.Namespace(
                factory_root=self.factory,
                product_root=self.product,
                project="relay",
                root=self.root,
            ))

    def test_hydrates_historical_pr_objects_once_without_moving_refs(self) -> None:
        publisher = self.workspace / "publisher"
        remote = self.workspace / "history.git"
        run(self.workspace, "git", "init", "--bare", "-q", str(remote))
        run(self.workspace, "git", "init", "-q", "-b", "main", str(publisher))
        run(publisher, "git", "config", "user.name", "Test")
        run(publisher, "git", "config", "user.email", "test@example.invalid")
        (publisher / "factory").mkdir()
        (publisher / "factory/PROJECT.env").write_text(
            "GH_REPO=example/product\n", encoding="utf-8",
        )
        run(publisher, "git", "add", ".")
        run(publisher, "git", "commit", "-qm", "base")
        run(publisher, "git", "remote", "add", "origin", str(remote))
        run(publisher, "git", "push", "-q", "origin", "main")
        run(publisher, "git", "switch", "-qc", "ticket/T-030")
        (publisher / "evidence.txt").write_text("evidence\n", encoding="utf-8")
        run(publisher, "git", "add", ".")
        run(publisher, "git", "commit", "-qm", "evidence")
        evidence = run(publisher, "git", "rev-parse", "HEAD")
        (publisher / "tip.txt").write_text("tip\n", encoding="utf-8")
        run(publisher, "git", "add", ".")
        run(publisher, "git", "commit", "-qm", "tip")
        head = run(publisher, "git", "rev-parse", "HEAD")
        run(
            publisher, "git", "push", "-q", "origin",
            f"HEAD:refs/pull/30/head",
        )
        run(publisher, "git", "switch", "-q", "main")
        migration = publisher / "factory/migrations/protected-merge-reconciliation/T-030.json"
        migration.parent.mkdir(parents=True)
        migration.write_text(json.dumps({
            "adoption_pr": {"head": head, "number": 30},
            "evidence_head": evidence,
            "original_pr": {"head": head, "number": 30},
            "repository": "example/product",
            "schema": "nysa.software-factory.protected-merge-reconciliation/v1",
        }) + "\n", encoding="utf-8")
        run(publisher, "git", "add", ".")
        run(publisher, "git", "commit", "-qm", "record migration")
        run(publisher, "git", "push", "-q", "origin", "main")

        consumer = self.workspace / "consumer"
        run(
            self.workspace, "git", "clone", "-q", "--no-local",
            "--single-branch", "--branch", "main", str(remote), str(consumer),
        )
        self.assertFalse(ENVIRONMENT.commit_present(consumer, head))
        refs = run(consumer, "git", "show-ref")
        self.assertEqual(ENVIRONMENT.historical_pr_objects(consumer), 1)
        self.assertTrue(ENVIRONMENT.commit_present(consumer, head))
        self.assertTrue(ENVIRONMENT.commit_present(consumer, evidence))
        self.assertEqual(run(consumer, "git", "show-ref"), refs)
        run(consumer, "git", "remote", "set-url", "origin", "invalid://offline")
        self.assertEqual(ENVIRONMENT.historical_pr_objects(consumer), 1)

    def test_historical_pr_ref_mismatch_fails_closed(self) -> None:
        migrations = self.product / "factory/migrations/contract-1.3"
        migrations.mkdir(parents=True)
        (self.product / "factory/PROJECT.env").write_text(
            "GH_REPO=example/product\n", encoding="utf-8",
        )
        (migrations / "T-013.json").write_text(json.dumps({
            "pr": {"head": "f" * 40, "number": 13},
            "repository": "example/product",
            "schema": "nysa.software-factory.legacy-closeout/v1",
        }) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError,
            r"historical PR head unavailable: .*T-013.json PR #13",
        ):
            ENVIRONMENT.historical_pr_objects(self.product)
        self.assertFalse((self.root / "marker.json").exists())

    def test_takeover_reuses_authenticated_live_state_without_copying_it(self) -> None:
        source_sha = "b" * 40
        intermediate_sha = "d" * 40
        tickets = ["T-094", "T-100", "T-093"]
        (self.product / "factory/KIT_PIN").write_text(
            source_sha + "\n", encoding="utf-8",
        )
        (self.product / ".gitignore").write_text(
            "factory/linear-map.json\n", encoding="utf-8",
        )
        for ticket in tickets:
            (self.product / f"factory/tickets/{ticket}.md").write_text(
                f"# {ticket}\n\nState: Ready\nProduct-Decisions: frozen\n"
                "Initiative: I-001\nDepends-On: none\nFixture-Seams: none\n"
                "Authentication-Seams: none\nProtected-Test-Conflicts: none\n"
                "Builder ownership: app/server.js only\n",
                encoding="utf-8",
            )
        run(
            self.product, "git", "add", "factory/KIT_PIN", ".gitignore",
            "factory/tickets",
        )
        run(self.product, "git", "commit", "-qm", "protected source")
        protected_sha = run(self.product, "git", "rev-parse", "HEAD")
        protected_tree = run(self.product, "git", "rev-parse", "HEAD^{tree}")
        run(
            self.product, "git", "update-ref", "refs/remotes/origin/main",
            protected_sha,
        )
        source_product = self.workspace / "source-product"
        run(
            self.product, "git", "worktree", "add", "-q", "--detach",
            str(source_product), protected_sha,
        )
        operator_map = source_product / "factory/linear-map.json"
        ENVIRONMENT.write(operator_map, {"last_success_at": "2026-07-31T12:00:00Z"})
        (self.product / "shared-policy.txt").write_text(
            "protected control change\n", encoding="utf-8",
        )
        run(self.product, "git", "add", "shared-policy.txt")
        run(self.product, "git", "commit", "-qm", "advance protected policy")
        current_protected_sha = run(self.product, "git", "rev-parse", "HEAD")
        run(
            self.product, "git", "update-ref", "refs/remotes/origin/main",
            current_protected_sha,
        )
        (self.product / "factory/KIT_PIN").write_text(
            self.sha + "\n", encoding="utf-8",
        )
        (self.product / "factory/QUALIFICATION.json").write_text(json.dumps({
            "budget_usd": "300.000000",
            "capacity": 3,
            "contract_version": "1.8.0",
            "factory_sha": self.sha,
            "generation": 1,
            "mode": "successor",
            "per_run_budget_usd": "10.000000",
            "per_ticket_budget_usd": "100.000000",
            "schema": "nysa.software-factory.qualification/v2",
            "source_factory_sha": source_sha,
            "target_done": 3,
            "tickets": tickets,
        }, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        run(
            self.product, "git", "add", "factory/KIT_PIN",
            "factory/QUALIFICATION.json", "factory/tickets",
        )
        run(self.product, "git", "commit", "-qm", "authorize qualification")

        account = (self.workspace / "account").resolve()
        provider = account / ".factory"
        kits = provider / "kits"
        source = kits / "projects/relay"
        state = source / "controller"
        passports = state / "passports"
        for path in (
            provider, kits, kits / "projects", source, state, passports,
            provider / "accounting", provider / "cli-runtimes",
            provider / "provider-attempts", provider / "provider-apply-locks",
        ):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.chmod(0o700)
        ENVIRONMENT.write(source / "active.json", {
            "contract_version": "1.8.0",
            "kit_sha": source_sha,
            "kit_tree": "c" * 40,
            "product_path": str(source_product.resolve()),
            "product_tree": protected_tree,
            "project": "relay",
        })
        secret = b"p" * 32
        key = state / "passport.key"
        key.write_bytes(secret)
        key.chmod(0o600)
        for ticket in tickets:
            body = {
                "completed_role_evidence": ([{
                    "contract_version": "1.8.0",
                    "factory_sha": source_sha,
                    "head_before": "1" * 40,
                    "manifest_sha256": "2" * 64,
                    "output_sha256": "3" * 64,
                    "role": "test-author",
                    "run_id": "missing-terminal-run",
                    "transition_receipt_sha256": "4" * 64,
                }] if ticket == "T-094" else []),
                "factory_release_history": [{
                    "contract_version": "1.8.0",
                    "factory_sha": source_sha,
                }, {
                    "contract_version": "1.8.0",
                    "factory_sha": intermediate_sha,
                }],
                "factory_sha": intermediate_sha,
                "migration_history": [{
                    "from_factory_sha": source_sha,
                    "from_head_sha": "1" * 40,
                    "from_passport_file_sha256": "2" * 64,
                    "from_passport_sha256": "3" * 64,
                    "from_protected_base_sha": "4" * 40,
                    "from_route_plan_sha256": "5" * 64,
                    "schema": "nysa.software-factory.ticket-passport-migration/v2",
                    "to_factory_sha": intermediate_sha,
                    "to_head_sha": "6" * 40,
                    "to_protected_base_sha": "7" * 40,
                    "to_route_plan_sha256": "8" * 64,
                }],
                "project": "relay",
                "schema": "nysa.software-factory.ticket-passport/v1",
                "ticket": ticket,
            }
            authenticated = dict(body)
            authenticated["authentication_sha256"] = hmac.new(
                secret, ENVIRONMENT.canonical(body), hashlib.sha256
            ).hexdigest()
            authenticated["passport_sha256"] = hashlib.sha256(
                ENVIRONMENT.canonical(authenticated)
            ).hexdigest()
            path = passports / f"{ticket}.json"
            path.write_bytes(ENVIRONMENT.canonical(authenticated))
            path.chmod(0o600)
        policy, activation, _ = ENVIRONMENT.provider_configuration(self.factory)
        ENVIRONMENT.write(provider / "provider-policy.json", policy)
        ENVIRONMENT.write(provider / "isolated-v1.enabled", activation)
        configuration_lock = provider / "provider-configuration.lock"
        configuration_lock.touch(mode=0o600)
        configuration_lock.chmod(0o600)
        run(
            provider,
            "/usr/bin/python3", str(self.factory / "scripts/provider-coordinator.py"),
            "--db", str(provider / "accounting/state-v2.sqlite3"), "status",
        )

        args = argparse.Namespace(
            factory_root=self.factory,
            product_root=self.product,
            project="relay",
            root=self.root,
            takeover_project="relay",
        )
        (self.product / "factory/runs").mkdir(mode=0o700)
        with (
            mock.patch.object(Path, "home", return_value=account),
            mock.patch.object(
                ENVIRONMENT, "protected_terminal",
                side_effect=ENVIRONMENT.TerminalError("not terminal"),
            ),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "missing-terminal-run test-author missing meta",
            ),
        ):
            ENVIRONMENT.takeover_source(
                self.factory, self.product.resolve(), "relay", "relay",
            )
        with (
            mock.patch.object(Path, "home", return_value=account),
            mock.patch.object(
                ENVIRONMENT, "protected_terminal",
                side_effect=(
                    {},
                    ENVIRONMENT.TerminalError("not terminal"),
                    ENVIRONMENT.TerminalError("not terminal"),
                ),
            ),
        ):
            value = ENVIRONMENT.prepare(args)

        active = json.loads((self.root / "projects/relay/active.json").read_text())
        self.assertEqual(value["qualification_mode"], "takeover")
        self.assertEqual(active["qualification_mode"], "takeover")
        self.assertEqual(active["takeover_kits_root"], str(kits))
        self.assertEqual(active["operator_map_path"], str(operator_map.resolve()))
        self.assertFalse((self.product / "factory/linear-map.json").exists())
        self.assertFalse((self.root / "provider").exists())
        self.assertFalse((self.root / "projects/relay/controller").exists())

        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "does not match active product",
        ):
            ENVIRONMENT.validate_takeover_product(
                source_product,
                self.product,
                {"product_tree": "0" * 40},
                {"tickets": tickets},
            )
        run(
            self.product, "git", "update-ref", "refs/remotes/origin/main",
            f"{protected_sha}^",
        )
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "does not contain the active product",
        ):
            ENVIRONMENT.validate_takeover_product(
                source_product,
                self.product,
                {"product_tree": protected_tree},
                {"tickets": tickets},
            )
        run(
            self.product, "git", "update-ref", "refs/remotes/origin/main",
            current_protected_sha,
        )

        unrelated = self.workspace / "unrelated-product"
        shutil.copytree(self.product, unrelated, ignore=shutil.ignore_patterns(".git"))
        run(unrelated, "git", "init", "-q", "-b", "main")
        run(unrelated, "git", "config", "user.name", "Test")
        run(unrelated, "git", "config", "user.email", "test@example.invalid")
        run(unrelated, "git", "remote", "add", "origin", "git@example.invalid")
        run(unrelated, "git", "add", ".")
        run(unrelated, "git", "commit", "-qm", "unrelated")
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "not a linked canonical worktree",
        ):
            ENVIRONMENT.validate_takeover_product(
                source_product,
                unrelated,
                {"product_tree": protected_tree},
                {"tickets": tickets},
            )
        (self.product / "application.txt").write_text("not control data\n")
        run(self.product, "git", "add", "application.txt")
        run(self.product, "git", "commit", "-qm", "change product code")
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "changes non-control product files",
        ):
            ENVIRONMENT.validate_takeover_product(
                source_product,
                self.product,
                {"product_tree": protected_tree},
                {"tickets": tickets},
            )

    def test_upgrades_release_without_replacing_controller_state(self) -> None:
        args = argparse.Namespace(
            factory_root=self.factory,
            product_root=self.product,
            project="relay",
            root=self.root,
        )
        first = ENVIRONMENT.prepare(args)
        active_path = self.root / "projects/relay/active.json"
        legacy_active = ENVIRONMENT.read(active_path)
        legacy_active.pop("product_sha")
        legacy_active.pop("runtime_tuple")
        ENVIRONMENT.replace(active_path, legacy_active)
        controller = Path(first["authority_root"]) / "controller"
        claims = controller / "claims"
        self.assertTrue(controller.is_dir())
        self.assertEqual(controller.stat().st_mode & 0o777, 0o700)
        claims.mkdir(mode=0o700)
        key = controller / "passport.key"
        key.write_bytes(b"p" * 32)
        key.chmod(0o600)
        ENVIRONMENT.write(claims / "T-110.json", {"status": "running"})

        (self.factory / "successor.txt").write_text("successor\n", encoding="utf-8")
        run(self.factory, "git", "add", "successor.txt")
        run(self.factory, "git", "commit", "-qm", "successor")
        successor = run(self.factory, "git", "rev-parse", "HEAD")
        (self.product / "factory/KIT_PIN").write_text(
            successor + "\n", encoding="utf-8",
        )
        run(self.product, "git", "add", "factory/KIT_PIN")
        run(self.product, "git", "commit", "-qm", "pin successor")

        original_global = (self.root / "global.env").read_bytes()
        self.global_env.write_text(
            "CLAUDE_CODE_PINNED=9.9.9\n", encoding="utf-8",
        )
        ENVIRONMENT.snapshot_global_config(args, self.root)
        self.assertEqual((self.root / "global.env").read_bytes(), original_global)
        replacement = self.workspace / "qualification-global.env"
        replacement.write_text(
            "CLAUDE_CODE_PINNED=2.1.224\n", encoding="utf-8",
        )
        replacement = replacement.resolve(strict=True)
        replacement.chmod(0o600)

        before = {
            "active": active_path.read_bytes(),
            "environment": (self.root / "environment.json").read_bytes(),
            "releases": sorted(path.name for path in (self.root / "releases").iterdir()),
            "receipts": sorted(path.name for path in (self.root / "receipts").iterdir()),
            "authority": (Path(first["authority_root"]) / "authority.json").read_bytes(),
        }
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError,
            "Contract 1.8 qualification manifest is invalid",
        ):
            ENVIRONMENT.upgrade(argparse.Namespace(
                **vars(args), global_env=replacement,
            ))
        self.assertEqual(active_path.read_bytes(), before["active"])
        self.assertEqual(
            (self.root / "environment.json").read_bytes(), before["environment"]
        )
        self.assertEqual(
            sorted(path.name for path in (self.root / "releases").iterdir()),
            before["releases"],
        )
        self.assertEqual(
            sorted(path.name for path in (self.root / "receipts").iterdir()),
            before["receipts"],
        )
        self.assertEqual(
            (Path(first["authority_root"]) / "authority.json").read_bytes(),
            before["authority"],
        )

        manifest_path = self.product / "factory/QUALIFICATION.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["factory_sha"] = successor
        manifest_path.write_text(json.dumps(manifest) + "\n")
        run(self.product, "git", "add", "factory/QUALIFICATION.json")
        run(self.product, "git", "commit", "-qm", "authorize successor")

        second = ENVIRONMENT.upgrade(argparse.Namespace(
            **vars(args), global_env=replacement,
        ))
        active = json.loads(active_path.read_text())
        self.assertEqual(first["status"], "prepared")
        self.assertEqual(second["status"], "upgraded")
        self.assertEqual(active["kit_sha"], successor)
        self.assertEqual(active["generation"], 2)
        self.assertEqual(active["product_sha"], second["product_sha"])
        self.assertEqual(active["runtime_tuple"], second["runtime_tuple"])
        self.assertEqual(
            (self.root / "global.env").read_bytes(), replacement.read_bytes(),
        )
        self.assertEqual(key.read_bytes(), b"p" * 32)
        self.assertTrue((self.root / f"releases/{self.sha}").is_dir())
        self.assertTrue((self.root / f"releases/{successor}").is_dir())

    def test_successor_upgrade_requires_exact_source_bound_cohort(self) -> None:
        controller = (self.workspace / "cohort-controller").resolve()
        passports = controller / "passports"
        passports.mkdir(mode=0o700, parents=True)
        controller.chmod(0o700)
        secret = b"p" * 32
        key = controller / "passport.key"
        key.write_bytes(secret)
        key.chmod(0o600)
        source = "b" * 40
        candidate = "c" * 40
        tickets = ["T-101", "T-102", "T-103"]
        manifest = {
            "factory_sha": candidate,
            "mode": "successor",
            "source_factory_sha": source,
            "tickets": tickets,
        }
        product_sha = run(self.product, "git", "rev-parse", "HEAD")
        for ticket in tickets:
            self.write_passport(
                passports / f"{ticket}.json", secret, ticket, source,
            )
        before = {
            path.name: path.read_bytes() for path in controller.rglob("*")
            if path.is_file()
        }
        ENVIRONMENT.validate_successor_upgrade_cohort(
            self.factory, self.product, controller, "relay", source,
            product_sha, manifest,
        )
        ENVIRONMENT.validate_successor_upgrade_cohort(
            self.factory, self.product, controller, "relay", source,
            product_sha, manifest,
        )
        self.assertEqual(before, {
            path.name: path.read_bytes() for path in controller.rglob("*")
            if path.is_file()
        })
        for ticket in tickets:
            self.write_passport(
                passports / f"{ticket}.json", secret, ticket, candidate, source,
            )
        ENVIRONMENT.validate_successor_upgrade_cohort(
            self.factory, self.product, controller, "relay", candidate,
            product_sha, manifest,
        )
        ENVIRONMENT.validate_successor_upgrade_cohort(
            self.factory, self.product, controller, "relay", candidate,
            product_sha, manifest,
        )

        for ticket in tickets:
            self.write_passport(
                passports / f"{ticket}.json", secret, ticket, source,
            )
        path = passports / "T-102.json"
        foreign = "d" * 40

        def rewrite(mutator: Callable[[dict[str, object]], None]) -> None:
            value = json.loads(path.read_text(encoding="utf-8"))
            value.pop("authentication_sha256")
            value.pop("passport_sha256")
            mutator(value)
            self.sign_passport(path, secret, value)

        def charge(factory_sha: str, run_id: str) -> dict[str, object]:
            return {
                "accounting_state": "completed",
                "charge_micro_usd": 1,
                "contract_version": "1.8.0",
                "factory_sha": factory_sha,
                "head_before": "1" * 40,
                "manifest_sha256": "2" * 64,
                "role": "builder",
                "run_id": run_id,
                "transition_receipt_sha256": "3" * 64,
            }

        def completion(factory_sha: str, run_id: str) -> dict[str, object]:
            return {
                "contract_version": "1.8.0",
                "factory_sha": factory_sha,
                "head_before": "1" * 40,
                "manifest_sha256": "2" * 64,
                "output_sha256": "4" * 64,
                "role": "builder",
                "run_id": run_id,
                "transition_receipt_sha256": "3" * 64,
            }

        def conservative_records() -> tuple[dict[str, object], dict[str, object]]:
            runs = self.product / "factory/runs"
            runs.mkdir(mode=0o700, exist_ok=True)
            output = runs / "conservative.out"
            output.write_bytes(b"successful output\n")
            output.chmod(0o600)
            output_digest = hashlib.sha256(output.read_bytes()).hexdigest()
            fields = {
                "accounting_state": "abandoned_conservative",
                "contract_version": "1.8.0",
                "cost_basis": "conservative_reservation",
                "effective_cost": "2.000000",
                "exit_status": "0",
                "kit_sha": source,
                "output_sha256": output_digest,
                "phase": "completed",
                "reserved_usd": "2.000000",
                "role": "builder",
                "role_exit": "ok",
                "role_head_before": "1" * 40,
                "run_id": "conservative",
                "task_submitted": "1",
                "ticket": "T-102",
                "transition_receipt_sha256": "3" * 64,
            }
            meta = runs / "conservative.meta"
            meta.write_text("".join(
                f"{name}={value}\n" for name, value in sorted(fields.items())
            ))
            meta.chmod(0o600)
            digest = hashlib.sha256(meta.read_bytes()).hexdigest()
            charge_value = charge(source, "conservative")
            charge_value.update({
                "accounting_state": "abandoned_conservative",
                "charge_micro_usd": 2_000_000,
                "manifest_sha256": digest,
            })
            completion_value = completion(source, "conservative")
            completion_value.update({
                "manifest_sha256": digest,
                "output_sha256": output_digest,
            })
            return charge_value, completion_value

        def set_charge(value: dict[str, object], factory_sha: str) -> None:
            value["charge_records"] = [charge(factory_sha, "run-charge")]
            value["cumulative_charges_micro_usd"] = 1

        def set_completion(
            value: dict[str, object], factory_sha: str,
            charge_factory_sha: str = source,
        ) -> None:
            value["charge_records"] = [
                charge(charge_factory_sha, "run-completed")
            ]
            value["completed_role_evidence"] = [
                completion(factory_sha, "run-completed")
            ]
            value["cumulative_charges_micro_usd"] = 1

        rewrite(lambda value: set_completion(value, source))
        ENVIRONMENT.validate_successor_upgrade_cohort(
            self.factory, self.product, controller, "relay", source,
            product_sha, manifest,
        )
        conservative_charge, conservative_completion = conservative_records()
        rewrite(lambda value: value.update(
            charge_records=[conservative_charge],
            completed_role_evidence=[conservative_completion],
            cumulative_charges_micro_usd=2_000_000,
        ))
        ENVIRONMENT.validate_successor_upgrade_cohort(
            self.factory, self.product, controller, "relay", source,
            product_sha, manifest,
        )
        meta = self.product / "factory/runs/conservative.meta"
        meta.write_text(meta.read_text().replace("exit_status=0", "exit_status=1"))
        failed_digest = hashlib.sha256(meta.read_bytes()).hexdigest()
        failed_charge = dict(conservative_charge, manifest_sha256=failed_digest)
        failed_completion = dict(
            conservative_completion, manifest_sha256=failed_digest,
        )
        rewrite(lambda value: value.update(
            charge_records=[failed_charge],
            completed_role_evidence=[failed_completion],
            cumulative_charges_micro_usd=2_000_000,
        ))
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError,
            "T-102: successor qualification requires every selected ticket",
        ):
            ENVIRONMENT.validate_successor_upgrade_cohort(
                self.factory, self.product, controller, "relay", source,
                product_sha, manifest,
            )
        conservative_charge, conservative_completion = conservative_records()
        for accounting_state in ("cancelled", "cancelled_conservative"):
            with self.subTest(accounting_state=accounting_state):
                refused_charge = dict(conservative_charge)
                refused_charge["accounting_state"] = accounting_state
                rewrite(lambda value: value.update(
                    charge_records=[refused_charge],
                    completed_role_evidence=[conservative_completion],
                    cumulative_charges_micro_usd=2_000_000,
                ))
                with self.assertRaisesRegex(
                    ENVIRONMENT.EnvironmentError,
                    "T-102: successor qualification requires every selected ticket",
                ):
                    ENVIRONMENT.validate_successor_upgrade_cohort(
                        self.factory, self.product, controller, "relay", source,
                        product_sha, manifest,
                    )

        for ticket in tickets:
            self.write_passport(
                passports / f"{ticket}.json", secret, ticket, candidate, source,
            )
        rewrite(lambda value: set_completion(value, candidate, candidate))
        rewrite(lambda value: value.update(completed_role_corrections=[{
            "failed_factory_sha": candidate,
            "issue": "https://github.com/nysa-company/software-factory/issues/218",
            "output_head_sha": "5" * 40,
            "progress_events": 1,
            "progress_journal_sha256": "6" * 64,
            "receipt_parent_file_sha256": "7" * 64,
            "recovery_factory_sha": source,
            "run_id": "run-completed",
            "schema": "nysa.software-factory.completed-role-correction/v1",
            "transition_receipt_sha256": "3" * 64,
        }]))
        ENVIRONMENT.validate_successor_upgrade_cohort(
            self.factory, self.product, controller, "relay", candidate,
            product_sha, manifest,
        )
        ENVIRONMENT.validate_successor_upgrade_cohort(
            self.factory, self.product, controller, "relay", candidate,
            product_sha, manifest,
        )
        for ticket in tickets:
            self.write_passport(
                passports / f"{ticket}.json", secret, ticket, source,
            )

        cases = {
            "candidate-native": lambda: self.write_passport(
                path, secret, "T-102", candidate,
            ),
            "foreign-source": lambda: self.write_passport(
                path, secret, "T-102", foreign, source,
            ),
            "malformed-migration": lambda: rewrite(
                lambda value: value.update(migration_history=[{"schema": "bad"}])
            ),
            "candidate-charge": lambda: rewrite(
                lambda value: set_charge(value, candidate)
            ),
            "foreign-charge": lambda: rewrite(
                lambda value: set_charge(value, foreign)
            ),
            "candidate-completed": lambda: rewrite(
                lambda value: set_completion(value, candidate)
            ),
            "foreign-completed": lambda: rewrite(
                lambda value: set_completion(value, foreign)
            ),
            "missing": path.unlink,
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                self.write_passport(path, secret, "T-102", source)
                mutate()
                with self.assertRaisesRegex(
                    ENVIRONMENT.EnvironmentError,
                    ("selected cohort" if label == "missing" else "T-102")
                    + ": successor qualification requires every selected ticket",
                ):
                    ENVIRONMENT.validate_successor_upgrade_cohort(
                        self.factory, self.product, controller, "relay", source,
                        product_sha, manifest,
                    )
        self.write_passport(path, secret, "T-102", source)
        drifted = dict(manifest, source_factory_sha="e" * 40)
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError,
            "T-101: successor qualification requires every selected ticket",
        ):
            ENVIRONMENT.validate_successor_upgrade_cohort(
                self.factory, self.product, controller, "relay", source,
                product_sha, drifted,
            )

    def test_successor_accepts_only_exact_source_terminal_reconciliations(self) -> None:
        controller = (self.workspace / "terminal-controller").resolve()
        passports = controller / "passports"
        events = controller / "events"
        for path in (controller, passports, events):
            path.mkdir(mode=0o700)
        secret = b"p" * 32
        key = controller / "passport.key"
        key.write_bytes(secret)
        key.chmod(0o600)
        self.write_passport(passports / "T-103.json", secret, "T-103", self.sha)
        attestations = self.product / "factory/attestations"
        for ticket in ("T-101", "T-102"):
            ticket_path = self.product / f"factory/tickets/{ticket}.md"
            ticket_path.write_text(
                ticket_path.read_text().replace("State: Ready", "State: Done")
            )
            root = attestations / ticket
            root.mkdir(parents=True)
            (root / "done.json").write_text(json.dumps({
                "schema": "nysa.software-factory.ticket-done/v1",
                "ticket": ticket,
            }) + "\n")
        run(self.product, "git", "add", "factory")
        run(self.product, "git", "commit", "-qm", "record source terminals")
        source_product_sha = run(self.product, "git", "rev-parse", "HEAD")
        run(
            self.product, "git", "update-ref", "refs/remotes/origin/main",
            source_product_sha,
        )
        source_manifest = json.loads(run(
            self.product, "git", "show",
            f"{source_product_sha}:factory/QUALIFICATION.json",
        ))
        source_manifest_sha256 = hashlib.sha256(json.dumps(
            source_manifest, ensure_ascii=True, sort_keys=True,
            separators=(",", ":"),
        ).encode()).hexdigest()
        source_tree = run(
            self.product, "git", "rev-parse", f"{source_product_sha}^{{tree}}",
        )
        event_paths: dict[str, Path] = {}
        original_events: dict[str, dict[str, object]] = {}
        for epoch, ticket in enumerate(("T-101", "T-102"), 1):
            done = json.loads(run(
                self.product, "git", "show",
                f"{source_product_sha}:factory/attestations/{ticket}/done.json",
            ))
            value: dict[str, object] = {
                "done_sha256": hashlib.sha256(json.dumps(
                    done, ensure_ascii=True, sort_keys=True,
                    separators=(",", ":"),
                ).encode()).hexdigest(),
                "event": "protected_terminal_reconciled",
                "factory_sha": self.sha,
                "observed_at_epoch_ns": epoch,
                "protected_main_sha": source_product_sha,
                "protected_main_tree": source_tree,
                "protected_ticket_blob": run(
                    self.product, "git", "rev-parse",
                    f"{source_product_sha}:factory/tickets/{ticket}.md",
                ),
                "qualification_charge_micro_usd": 0,
                "qualification_generation": source_manifest["generation"],
                "qualification_manifest_sha256": source_manifest_sha256,
                "reconciliation_schema": (
                    "nysa.software-factory.qualification-protected-terminal-"
                    "reconciliation/v1"
                ),
                "schema": "nysa.software-factory.controller-event/v1",
                "terminal_basis": "attested-done",
                "ticket": ticket,
            }
            unsigned = json.dumps(
                value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
            ).encode()
            value["event_sha256"] = hashlib.sha256(unsigned).hexdigest()
            event_paths[ticket] = events / f"{epoch}-000000000000000{epoch}.json"
            original_events[ticket] = value
            ENVIRONMENT.write(event_paths[ticket], value)
        (self.product / "unrelated.txt").write_text("later protected change\n")
        run(self.product, "git", "add", "unrelated.txt")
        run(self.product, "git", "commit", "-qm", "advance protected main")
        moved_protected = run(self.product, "git", "rev-parse", "HEAD")
        run(
            self.product, "git", "update-ref", "refs/remotes/origin/main",
            source_product_sha,
        )

        candidate = "c" * 40
        manifest = {
            "factory_sha": candidate,
            "mode": "successor",
            "source_factory_sha": self.sha,
            "tickets": ["T-101", "T-102", "T-103"],
        }

        def restore_events() -> None:
            for path in events.glob("*.json"):
                path.unlink()
            for ticket, value in original_events.items():
                ENVIRONMENT.write(event_paths[ticket], value)

        def change_event(ticket: str, name: str, value: object) -> None:
            event = dict(original_events[ticket])
            event.pop("event_sha256")
            event[name] = value
            unsigned = json.dumps(
                event, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
            ).encode()
            event["event_sha256"] = hashlib.sha256(unsigned).hexdigest()
            event_paths[ticket].write_bytes(ENVIRONMENT.canonical(event))

        terminal_refs: list[str] = []
        move_ref = [True]

        def terminal(_product: Path, ticket: str, ref: str) -> dict[str, str]:
            terminal_refs.append(ref)
            if move_ref:
                move_ref.pop()
                run(
                    self.product, "git", "update-ref",
                    "refs/remotes/origin/main", moved_protected,
                )
            return {"basis": "attested-done", "ticket": ticket}

        before = {
            str(path.relative_to(controller)): path.read_bytes()
            for path in controller.rglob("*") if path.is_file()
        }
        with mock.patch.object(
            ENVIRONMENT, "protected_terminal", side_effect=terminal,
        ):
            ENVIRONMENT.validate_successor_upgrade_cohort(
                self.factory, self.product, controller, "relay", self.sha,
                source_product_sha, manifest,
            )
            run(
                self.product, "git", "update-ref", "refs/remotes/origin/main",
                source_product_sha,
            )
            ENVIRONMENT.validate_successor_upgrade_cohort(
                self.factory, self.product, controller, "relay", self.sha,
                source_product_sha, manifest,
            )
        self.assertEqual(set(terminal_refs), {source_product_sha})
        self.assertEqual(before, {
            str(path.relative_to(controller)): path.read_bytes()
            for path in controller.rglob("*") if path.is_file()
        })

        mutations = {
            "manifest": ("qualification_manifest_sha256", "0" * 64),
            "source": ("factory_sha", "b" * 40),
            "charge": ("qualification_charge_micro_usd", 1),
            "basis": ("terminal_basis", "attested-emergency-closeout"),
            "done": ("done_sha256", "0" * 64),
            "tree": ("protected_main_tree", "0" * 40),
        }
        for label, (name, value) in mutations.items():
            with self.subTest(label=label):
                restore_events()
                change_event("T-101", name, value)
                with mock.patch.object(
                    ENVIRONMENT, "protected_terminal", side_effect=terminal,
                ), self.assertRaisesRegex(
                    ENVIRONMENT.EnvironmentError,
                    "successor qualification requires every selected ticket",
                ):
                    ENVIRONMENT.validate_successor_upgrade_cohort(
                        self.factory, self.product, controller, "relay", self.sha,
                        source_product_sha, manifest,
                    )

        restore_events()
        ENVIRONMENT.write(events / "3-0000000000000003.json", original_events["T-101"])
        with mock.patch.object(
            ENVIRONMENT, "protected_terminal", side_effect=terminal,
        ), self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError,
            "successor qualification requires every selected ticket",
        ):
            ENVIRONMENT.validate_successor_upgrade_cohort(
                self.factory, self.product, controller, "relay", self.sha,
                source_product_sha, manifest,
            )

        restore_events()
        self.write_passport(passports / "T-101.json", secret, "T-101", self.sha)
        with mock.patch.object(
            ENVIRONMENT, "protected_terminal", side_effect=terminal,
        ), self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError,
            "successor qualification requires every selected ticket",
        ):
            ENVIRONMENT.validate_successor_upgrade_cohort(
                self.factory, self.product, controller, "relay", self.sha,
                source_product_sha, manifest,
            )
        (passports / "T-101.json").unlink()

        ticket = self.product / "factory/tickets/T-101.md"
        ticket.write_text(ticket.read_text() + "\nchanged after source\n")
        run(self.product, "git", "add", "factory/tickets/T-101.md")
        run(self.product, "git", "commit", "-qm", "change terminal ticket")
        run(
            self.product, "git", "update-ref", "refs/remotes/origin/main",
            run(self.product, "git", "rev-parse", "HEAD"),
        )
        with mock.patch.object(
            ENVIRONMENT, "protected_terminal", side_effect=terminal,
        ), self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError,
            "successor qualification requires every selected ticket",
        ):
            ENVIRONMENT.validate_successor_upgrade_cohort(
                self.factory, self.product, controller, "relay", self.sha,
                source_product_sha, manifest,
            )

    def test_successor_migration_gap_requires_completed_role_chain(self) -> None:
        base = run(self.product, "git", "rev-parse", "HEAD")
        test_path = self.product / "app/tests/feature.test.js"
        test_path.parent.mkdir(parents=True)
        test_path.write_text("test\n")
        run(self.product, "git", "add", "app/tests/feature.test.js")
        run(self.product, "git", "commit", "-qm", "test-author output")
        middle = run(self.product, "git", "rev-parse", "HEAD")
        builder_path = self.product / "app/server.js"
        builder_path.write_text("build\n")
        run(self.product, "git", "add", "app/server.js")
        run(self.product, "git", "commit", "-qm", "builder output")
        end = run(self.product, "git", "rev-parse", "HEAD")
        source = "b" * 40

        def completion(role: str, head: str, run_id: str) -> dict[str, object]:
            return {
                "contract_version": "1.8.0",
                "factory_sha": source,
                "head_before": head,
                "manifest_sha256": ("1" if role == "test-author" else "2") * 64,
                "output_sha256": "3" * 64,
                "role": role,
                "run_id": run_id,
                "transition_receipt_sha256": "4" * 64,
            }

        completed = [
            completion("test-author", base, "test-run"),
            completion("builder", middle, "builder-run"),
        ]
        charges = [{
            "accounting_state": "completed",
            "charge_micro_usd": 1,
            **{
                name: item[name] for name in (
                    "contract_version", "factory_sha", "head_before",
                    "manifest_sha256", "role", "run_id",
                    "transition_receipt_sha256",
                )
            },
        } for item in completed]
        passport_spec = importlib.util.spec_from_file_location(
            "gap_passport", self.factory / "scripts/ticket-passport.py"
        )
        assert passport_spec and passport_spec.loader
        passport = importlib.util.module_from_spec(passport_spec)
        passport_spec.loader.exec_module(passport)
        self.assertTrue(ENVIRONMENT.completed_role_gap(
            self.factory, self.product, passport, "T-101",
            charges, completed, base, end, source,
        ))
        negatives = {
            "missing": (charges, completed[1:]),
            "foreign": (
                charges,
                [{**completed[0], "factory_sha": "c" * 40}, completed[1]],
            ),
            "cancelled": (
                [{**charges[0], "accounting_state": "cancelled"}, charges[1]],
                completed,
            ),
            "head-gap": (
                charges,
                [{**completed[0], "head_before": middle}, completed[1]],
            ),
        }
        for label, (case_charges, case_completed) in negatives.items():
            with self.subTest(label=label):
                self.assertFalse(ENVIRONMENT.completed_role_gap(
                    self.factory, self.product, passport, "T-101",
                    case_charges, case_completed, base, end, source,
                ))

    def test_candidate_native_successor_refuses_before_upgrade_publication(self) -> None:
        args = argparse.Namespace(
            factory_root=self.factory,
            product_root=self.product,
            project="relay",
            root=self.root,
        )
        first = ENVIRONMENT.prepare(args)
        active_path = self.root / "projects/relay/active.json"
        authority = Path(first["authority_root"])

        (self.factory / "successor.txt").write_text("successor\n", encoding="utf-8")
        run(self.factory, "git", "add", "successor.txt")
        run(self.factory, "git", "commit", "-qm", "successor")
        successor = run(self.factory, "git", "rev-parse", "HEAD")
        manifest_path = self.product / "factory/QUALIFICATION.json"
        manifest = json.loads(manifest_path.read_text())
        manifest.update({
            "budget_usd": "300.000000",
            "factory_sha": successor,
            "mode": "successor",
            "per_run_budget_usd": "10.000000",
            "per_ticket_budget_usd": "100.000000",
            "source_factory_sha": self.sha,
        })
        (self.product / "factory/KIT_PIN").write_text(successor + "\n")
        manifest_path.write_text(json.dumps(manifest) + "\n")
        run(
            self.product, "git", "add", "factory/KIT_PIN",
            "factory/QUALIFICATION.json",
        )
        run(self.product, "git", "commit", "-qm", "authorize successor")
        before_root = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*") if path.is_file()
        }
        before_authority = {
            path.relative_to(authority): path.read_bytes()
            for path in authority.rglob("*") if path.is_file()
        }

        with (
            mock.patch.object(
                ENVIRONMENT, "resume_operator_state",
                side_effect=AssertionError("operator state must not change"),
            ),
            mock.patch.object(
                ENVIRONMENT, "initialize_selected_linear",
                side_effect=AssertionError("Linear must not run"),
            ),
            mock.patch.object(
                ENVIRONMENT, "materialize",
                side_effect=AssertionError("successor must not be sealed"),
            ),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError,
                "successor qualification requires every selected ticket",
            ),
        ):
            ENVIRONMENT.upgrade(args)

        self.assertEqual(ENVIRONMENT.read(active_path)["kit_sha"], self.sha)
        self.assertEqual(before_root, {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*") if path.is_file()
        })
        self.assertEqual(before_authority, {
            path.relative_to(authority): path.read_bytes()
            for path in authority.rglob("*") if path.is_file()
        })
        self.assertFalse((self.root / f"releases/{successor}").exists())

    def test_normal_upgrade_refuses_terminal_target_before_any_mutation(self) -> None:
        args = argparse.Namespace(
            factory_root=self.factory,
            product_root=self.product,
            project="relay",
            root=self.root,
        )
        ENVIRONMENT.prepare(args)
        (self.factory / "successor.txt").write_text("successor\n", encoding="utf-8")
        run(self.factory, "git", "add", "successor.txt")
        run(self.factory, "git", "commit", "-qm", "successor")
        successor = run(self.factory, "git", "rev-parse", "HEAD")
        (self.product / "factory/KIT_PIN").write_text(successor + "\n")
        manifest_path = self.product / "factory/QUALIFICATION.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["factory_sha"] = successor
        manifest_path.write_text(json.dumps(manifest) + "\n")
        run(
            self.product, "git", "add", "factory/KIT_PIN",
            "factory/QUALIFICATION.json",
        )
        run(self.product, "git", "commit", "-qm", "authorize successor")
        before = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*") if path.is_file()
        }

        with (
            mock.patch.object(
                ENVIRONMENT, "protected_terminal",
                return_value={"ticket": "T-110"},
            ),
            mock.patch.object(
                ENVIRONMENT, "initialize_selected_linear",
                side_effect=AssertionError("Linear must not run"),
            ),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError,
                "terminal qualification target requires a successor lane",
            ),
        ):
            ENVIRONMENT.upgrade(args)

        after = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*") if path.is_file()
        }
        self.assertEqual(after, before)
        self.assertFalse((self.root / f"releases/{successor}").exists())

    def test_restores_signed_safe_pause_after_disposable_root_is_removed(self) -> None:
        args = argparse.Namespace(
            factory_root=self.factory,
            product_root=self.product,
            project="relay",
            root=self.root,
        )
        first = ENVIRONMENT.prepare(args)
        authority = Path(first["authority_root"])
        controller = authority / "controller"
        parked = controller / "parked/T-101"
        parked.parent.mkdir(mode=0o700)
        run(self.product, "git", "branch", "ticket/T-101")
        run(
            self.product, "git", "worktree", "add", "-q", str(parked),
            "ticket/T-101",
        )
        head = run(parked, "git", "rev-parse", "HEAD")
        secret = b"p" * 32
        key = controller / "passport.key"
        key.write_bytes(secret)
        key.chmod(0o600)
        body = {
            "branch": "ticket/T-101",
            "current_stage": "RUN builder",
            "current_state": "Building",
            "factory_sha": self.sha,
            "head_sha": head,
            "project": "relay",
            "publication_state": "none",
            "schema": "nysa.software-factory.ticket-passport/v1",
            "ticket": "T-101",
        }
        passport = dict(body)
        passport["authentication_sha256"] = hmac.new(
            secret, ENVIRONMENT.canonical(body), hashlib.sha256,
        ).hexdigest()
        passport["passport_sha256"] = hashlib.sha256(
            ENVIRONMENT.canonical(passport)
        ).hexdigest()
        passports = controller / "passports"
        passports.mkdir(mode=0o700)
        ENVIRONMENT.write(passports / "T-101.json", passport)
        run_snapshot = hashlib.sha256(b"[]").hexdigest()
        pause = {
            "blocking_issue": "https://github.com/example/software-factory/issues/1",
            "branch": "ticket/T-101",
            "budget_sha256": None,
            "created_at_epoch": 1,
            "current_stage": "RUN builder",
            "current_state": "Building",
            "factory_sha": self.sha,
            "head_sha": head,
            "passport_sha256": passport["passport_sha256"],
            "passport_factory_sha": self.sha,
            "resume_state": None,
            "run_snapshot_sha256": run_snapshot,
            "schema": "nysa.software-factory.ticket-pause/v2",
            "status": "claimed",
            "ticket": "T-101",
            "worktree": str(parked),
        }
        pause["pause_sha256"] = hashlib.sha256(json.dumps(
            pause, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        ENVIRONMENT.write(controller / "pause-T-101.json", pause)

        for base, directories, files in os.walk(self.root, topdown=False):
            for name in files:
                (Path(base) / name).chmod(0o600)
            for name in directories:
                (Path(base) / name).chmod(0o700)
        shutil.rmtree(self.root)
        with mock.patch.object(
            ENVIRONMENT, "initialize_selected_linear",
            wraps=ENVIRONMENT.initialize_selected_linear,
        ) as initialize:
            restored = ENVIRONMENT.prepare(argparse.Namespace(
                **vars(args), restore=True,
            ))
        initialize.assert_called_once()
        self.assertIs(initialize.call_args.kwargs.get("refresh"), True)
        active = ENVIRONMENT.read(self.root / "projects/relay/active.json")
        self.assertEqual(restored["status"], "restored")
        self.assertEqual(active["controller_state_path"], str(controller))
        self.assertEqual(active["provider_state_path"], str(authority / "provider"))
        self.assertEqual(key.read_bytes(), secret)
        self.assertEqual(run(parked, "git", "rev-parse", "HEAD"), head)

    def handoff_fixture(
        self, *, released: bool = False, noncontrol: bool = False,
    ):
        remote = self.workspace / "handoff-remote.git"
        run(self.workspace, "git", "init", "--bare", "-q", str(remote))
        run(self.product, "git", "remote", "set-url", "origin", str(remote))
        (self.product / "factory/PROJECT.env").write_text(
            "MAX_CONCURRENT_TICKETS=3\n", encoding="utf-8",
        )
        with (self.product / ".gitignore").open("a", encoding="utf-8") as stream:
            stream.write("factory/.dispatch-leases/\nfactory/.active-runs/\n")
        run(self.product, "git", "add", ".")
        run(self.product, "git", "commit", "-qm", "handoff base")
        run(self.product, "git", "push", "-qu", "origin", "main")

        source_root = Path(tempfile.mkdtemp(
            prefix="nysa-sf-qualification.source-", dir="/private/tmp",
        )).resolve()
        target_root = Path(tempfile.mkdtemp(
            prefix="nysa-sf-qualification.target-", dir="/private/tmp",
        )).resolve()
        os.chmod(source_root, 0o700)
        os.chmod(target_root, 0o700)
        self.addCleanup(shutil.rmtree, source_root, True)
        self.addCleanup(shutil.rmtree, target_root, True)
        source_cells = source_root / "worktrees/source"
        source_cells.mkdir(mode=0o700, parents=True)
        source_cells.parent.chmod(0o700)
        target_product = self.workspace / "target-product"
        run(
            self.product, "git", "worktree", "add", "-qb", "qualification-target",
            str(target_product), "main",
        )
        target_product = target_product.resolve()
        tickets = ["T-101", "T-102", "T-103"]
        entries = []
        source_controller = self.home / ".factory/qualification/source/controller"
        target_controller = self.home / ".factory/qualification/target/controller"
        for path in (
            source_controller / "claims", target_controller / "claims",
        ):
            path.mkdir(mode=0o700, parents=True)
            path.parent.chmod(0o700)
        source_sha = "b" * 40
        for index, ticket in enumerate(tickets, 1):
            cell = source_cells / f"cell-{index}"
            run(
                self.product, "git", "worktree", "add", "-qb", f"ticket/{ticket}",
                str(cell), "main",
            )
            cell.chmod(0o700)
            ticket_path = cell / f"factory/tickets/{ticket}.md"
            ticket_path.write_text(
                ticket_path.read_text() + f"\nKit-SHA: {source_sha}\n",
                encoding="utf-8",
            )
            route = cell / f"factory/route-plans/{ticket}.json"
            route.parent.mkdir(exist_ok=True)
            route.write_text(json.dumps({
                "kit_sha": source_sha,
                "schema": "ticket-model-route-plan/v1",
                "ticket": ticket,
            }) + "\n", encoding="utf-8")
            if noncontrol and index == 1:
                unsafe = cell / "apps/unsafe.txt"
                unsafe.parent.mkdir()
                unsafe.write_text("not control state\n", encoding="utf-8")
            run(cell, "git", "add", ".")
            run(
                cell, "git", "-c", "user.name=Software Factory", "-c",
                "user.email=factory@local", "commit", "-qm",
                f"{ticket}: pin kit and model route plan",
            )
            ticket_path.write_text(
                ticket_path.read_text().replace("State: Ready", "State: Planning"),
                encoding="utf-8",
            )
            run(cell, "git", "add", str(ticket_path))
            run(
                cell, "git", "-c", "user.name=Software Factory", "-c",
                "user.email=factory@local", "commit", "-qm",
                f"{ticket}: transition ticket state",
            )
            run(cell, "git", "push", "-qu", "origin", f"ticket/{ticket}")
            head = run(cell, "git", "rev-parse", "HEAD")
            lease = hashlib.sha256(f"lease-{ticket}".encode()).hexdigest()
            claim = {
                "blocked_reason": "worker-error",
                "branch": f"ticket/{ticket}",
                "lease": lease,
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": "nysa.software-factory.controller-claim/v1",
                "status": "blocked",
                "ticket": ticket,
                "worktree": str(cell),
            }
            if released:
                claim["lease_released"] = True
            ENVIRONMENT.write(source_controller / f"claims/{ticket}.json", claim)
            lease_dir = self.product / "factory/.dispatch-leases"
            lease_dir.mkdir(mode=0o700, exist_ok=True)
            if not released:
                ENVIRONMENT.write(lease_dir / f"{ticket}.json", {
                    "claimed_epoch": 1,
                    "expires_epoch": 9999999999,
                    "lease_id": lease,
                    "schema_version": 1,
                    "ticket": ticket,
                })
            receipt = {
                "branch": f"ticket/{ticket}",
                "contract_version": "1.8.0",
                "evidence_sha256": "1" * 64,
                "factory_sha": source_sha,
                "head_sha": head,
                "head_tree": run(cell, "git", "rev-parse", "HEAD^{tree}"),
                "lease_sha256": hashlib.sha256(lease.encode()).hexdigest(),
                "loop": None,
                "nonce": f"{index:032x}",
                "passport_sha256": None,
                "product_origin_sha256": hashlib.sha256(str(remote).encode()).hexdigest(),
                "project": "source",
                "role": "planner",
                "route_plan_sha256": hashlib.sha256(route.read_bytes()).hexdigest(),
                "schema": ENVIRONMENT.TRANSITION_RECEIPT_SCHEMA,
                "stage": "RUN planner",
                "ticket": ticket,
                "ticket_blob": run(
                    cell, "git", "rev-parse", f"HEAD:factory/tickets/{ticket}.md",
                ),
            }
            receipt["receipt_sha256"] = hashlib.sha256(
                ENVIRONMENT.canonical(receipt)
            ).hexdigest()
            receipt["consumed"] = False
            ENVIRONMENT.write(source_controller / f"{ticket}.json", receipt)
            entries.append((ticket, head, cell))

        run(self.factory, "git", "commit", "--allow-empty", "-qm", "target kit")
        target_kit = run(self.factory, "git", "rev-parse", "HEAD")
        target_kit_tree = run(self.factory, "git", "rev-parse", "HEAD^{tree}")
        reset = target_product / "factory/qualification/preprovider-branch-resets.json"
        reset.parent.mkdir(exist_ok=True)
        reset.write_text(json.dumps({
            "factory_sha": target_kit,
            "resets": [
                {"branch": f"ticket/{ticket}", "head": head, "ticket": ticket}
                for ticket, head, _ in entries
            ],
            "schema": ENVIRONMENT.PREPROVIDER_RESET_SCHEMA,
        }, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        run(target_product, "git", "add", str(reset))
        run(target_product, "git", "commit", "-qm", "authorize handoff")
        run(
            target_product, "git", "push", "-q", "origin",
            "qualification-target:main",
        )
        target_sha = run(target_product, "git", "rev-parse", "HEAD")
        target_tree = run(target_product, "git", "rev-parse", "HEAD^{tree}")
        manifest = {"capacity": 3, "tickets": tickets}
        source = {
            "active": {
                "kit_sha": source_sha, "project": "source", "receipt_id": "2" * 64,
            },
            "authority": source_controller.parent,
            "controller": source_controller,
            "manifest": manifest,
            "product": self.product,
            "release": ROOT,
            "root": source_root,
        }
        target = {
            "active": {
                "kit_sha": target_kit, "kit_tree": target_kit_tree,
                "product_sha": target_sha, "product_tree": target_tree,
                "project": "target", "receipt_id": "3" * 64,
            },
            "authority": target_controller.parent,
            "controller": target_controller,
            "manifest": {
                **manifest, "mode": "successor", "source_factory_sha": source_sha,
            },
            "product": target_product,
            "release": ROOT,
            "root": target_root,
        }
        args = argparse.Namespace(
            factory_root=self.factory,
            preprovider_source_project="source",
            preprovider_source_root=source_root,
            product_root=target_product,
            project="target",
            restore=False,
            root=target_root,
            takeover_project=None,
            upgrade=False,
        )
        return args, source, target, entries

    def test_handoff_moves_active_leases_once_and_recovers_move_before_journal(self):
        args, source, target, entries = self.handoff_fixture()
        lanes = {source["root"]: source, target["root"]: target}
        with (
            mock.patch.object(
                ENVIRONMENT, "qualification_lane",
                side_effect=lambda root, project: lanes[Path(root)],
            ),
            mock.patch.object(ENVIRONMENT, "provider_drained"),
        ):
            first = ENVIRONMENT.handoff_preprovider(args)
            self.assertEqual(first["status"], "preprovider-handed-off")
            for ticket, _, source_cell in entries:
                claim = ENVIRONMENT.read(source["controller"] / f"claims/{ticket}.json")
                self.assertTrue(claim["lease_released"])
                self.assertEqual(claim["blocked_reason"], "preprovider-handoff")
                self.assertFalse(source_cell.exists())
                self.assertTrue(Path(claim["worktree"]).is_dir())
                self.assertFalse(
                    (source["product"] / f"factory/.dispatch-leases/{ticket}.json").exists()
                )
            journal_path = target["controller"] / "preprovider-handoff.json"
            journal = ENVIRONMENT.read(journal_path)
            journal["status"] = "prepared"
            journal["moved"] = journal["moved"][:-1]
            ENVIRONMENT.replace(journal_path, ENVIRONMENT.seal_journal(journal))
            repeated = ENVIRONMENT.handoff_preprovider(args)
            self.assertEqual(repeated["handoff_sha256"], first["handoff_sha256"])
            self.assertEqual(
                ENVIRONMENT.read(journal_path)["status"], "completed"
            )

    def test_handoff_accepts_already_released_lease_and_refuses_reverse_move(self):
        args, source, target, entries = self.handoff_fixture(released=True)
        lanes = {source["root"]: source, target["root"]: target}
        with (
            mock.patch.object(
                ENVIRONMENT, "qualification_lane",
                side_effect=lambda root, project: lanes[Path(root)],
            ),
            mock.patch.object(ENVIRONMENT, "provider_drained"),
        ):
            ENVIRONMENT.handoff_preprovider(args)
            claim = ENVIRONMENT.read(
                source["controller"] / f"claims/{entries[0][0]}.json"
            )
            run(
                target["product"], "git", "worktree", "move",
                claim["worktree"], str(entries[0][2]),
            )
            with self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "physical state conflicts"
            ):
                ENVIRONMENT.handoff_preprovider(args)

    def test_handoff_transient_refusal_does_not_publish_a_journal(self):
        args, source, target, entries = self.handoff_fixture()
        lanes = {source["root"]: source, target["root"]: target}
        dirty = entries[0][2] / "untracked.txt"
        dirty.write_text("transient\n", encoding="utf-8")
        journal = target["controller"] / "preprovider-handoff.json"
        with (
            mock.patch.object(
                ENVIRONMENT, "qualification_lane",
                side_effect=lambda root, project: lanes[Path(root)],
            ),
            mock.patch.object(ENVIRONMENT, "provider_drained"),
        ):
            with self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "source evidence changed"
            ):
                ENVIRONMENT.handoff_preprovider(args)
            self.assertFalse(journal.exists())
            dirty.unlink()
            self.assertEqual(
                ENVIRONMENT.handoff_preprovider(args)["status"],
                "preprovider-handed-off",
            )

    def test_sealed_reset_authorization_refuses_worktree_mutation(self):
        _, _, target, _ = self.handoff_fixture(released=True)
        path = target["product"] / (
            "factory/qualification/preprovider-branch-resets.json"
        )
        path.write_text(path.read_text() + " ", encoding="utf-8")
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "differs from sealed HEAD"
        ):
            ENVIRONMENT.preprovider_reset_authorizations(
                target["product"], target["active"]["kit_sha"],
                target["manifest"]["tickets"],
            )

    def test_handoff_refuses_busy_dispatch_admission_before_mutation(self):
        args, source, target, _ = self.handoff_fixture(released=True)
        lanes = {source["root"]: source, target["root"]: target}
        path = source["root"] / "worktrees/source/.dispatch-admission.lock"
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with (
                mock.patch.object(
                    ENVIRONMENT, "qualification_lane",
                    side_effect=lambda root, project: lanes[Path(root)],
                ),
                mock.patch.object(ENVIRONMENT, "provider_drained"),
                self.assertRaisesRegex(
                    ENVIRONMENT.EnvironmentError, "dispatch admission is active"
                ),
            ):
                ENVIRONMENT.handoff_preprovider(args)
        finally:
            os.close(descriptor)
        self.assertFalse(
            (target["controller"] / "preprovider-handoff.json").exists()
        )

    def test_handoff_refuses_target_runtime_before_journal(self):
        args, source, target, _ = self.handoff_fixture(released=True)
        lanes = {source["root"]: source, target["root"]: target}
        leases = target["product"] / "factory/.dispatch-leases"
        leases.mkdir(mode=0o700)
        ENVIRONMENT.write(leases / "T-101.json", {
            "claimed_epoch": 1,
            "expires_epoch": 9999999999,
            "lease_id": "9" * 64,
            "schema_version": 1,
            "ticket": "T-101",
        })
        with (
            mock.patch.object(
                ENVIRONMENT, "qualification_lane",
                side_effect=lambda root, project: lanes[Path(root)],
            ),
            mock.patch.object(ENVIRONMENT, "provider_drained"),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "runtime or publication evidence"
            ),
        ):
            ENVIRONMENT.handoff_preprovider(args)
        self.assertFalse(
            (target["controller"] / "preprovider-handoff.json").exists()
        )

    def test_handoff_refuses_authorized_noncontrol_head_before_journal(self):
        args, source, target, _ = self.handoff_fixture(
            released=True, noncontrol=True,
        )
        lanes = {source["root"]: source, target["root"]: target}
        with (
            mock.patch.object(
                ENVIRONMENT, "qualification_lane",
                side_effect=lambda root, project: lanes[Path(root)],
            ),
            mock.patch.object(ENVIRONMENT, "provider_drained"),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "not control-only"
            ),
        ):
            ENVIRONMENT.handoff_preprovider(args)
        self.assertFalse(
            (target["controller"] / "preprovider-handoff.json").exists()
        )
        for ticket in target["manifest"]["tickets"]:
            claim = ENVIRONMENT.read(source["controller"] / f"claims/{ticket}.json")
            self.assertEqual(claim["blocked_reason"], "worker-error")

    def test_handoff_refuses_activation_change_before_locked_revalidation(self):
        args, source, target, _ = self.handoff_fixture(released=True)
        changed = {
            **source,
            "active": {**source["active"], "receipt_id": "8" * 64},
        }
        with (
            mock.patch.object(
                ENVIRONMENT, "qualification_lane",
                side_effect=[source, target, changed, target],
            ),
            mock.patch.object(ENVIRONMENT, "provider_drained"),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "activation changed before handoff lock"
            ),
        ):
            ENVIRONMENT.handoff_preprovider(args)
        self.assertFalse(
            (target["controller"] / "preprovider-handoff.json").exists()
        )


if __name__ == "__main__":
    unittest.main()
