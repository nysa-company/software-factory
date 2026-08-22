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
import pwd
import re
import shutil
import subprocess
import sys
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
        runtime_bin = self.home / ".local/bin"
        runtime_bin.mkdir(parents=True, mode=0o700)
        for tool in ("node", "npm", "npx"):
            runtime_bin.joinpath(tool).symlink_to(shutil.which(tool))
        self.global_env = self.home / ".factory/global.env"
        self.global_env.write_text(
            "CLAUDE_CODE_PINNED=2.1.223\n"
            "GLOBAL_DAILY_CAP_USD=100.000000\n",
            encoding="utf-8",
        )
        self.global_env.chmod(0o600)
        os.environ["HOME"] = str(self.home)
        self.root = Path(tempfile.mkdtemp(
            prefix="nysa-sf-qualification.q-", dir="/private/tmp",
        )).resolve()
        os.chmod(self.root, 0o700)
        self.factory = self.workspace / "factory"
        (self.factory / "scripts").mkdir(parents=True)
        (self.factory / "factory-contract.json").write_text(
            '{"contract_version":"1.8.0"}\n', encoding="utf-8",
        )
        launcher = self.factory / "scripts/factory-launch"
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        launcher.chmod(0o755)
        factory_kit = self.factory / "scripts/factory-kit.sh"
        factory_kit.write_text(
            "#!/bin/sh\n"
            "if [ \"${FACTORY_TEST_PROVIDER_PIN_UNREADY:-0}\" = 1 ]; then\n"
            "  status=unready; item_status=error; reason=requested_release_not_approved\n"
            "else\n"
            "  status=ready; item_status=ok; reason=exact_pin_ready\n"
            "fi\n"
            "printf '%s\\n' '{\"schema\":\"nysa.software-factory.provider-cli-pin-status/v1\","
            "\"status\":\"'$status'\",\"requested_release\":{\"factory_sha\":\"'$4'\"},"
            "\"items\":["
            "{\"name\":\"claude\",\"status\":\"'$item_status'\",\"reason\":\"'$reason'\"},"
            "{\"name\":\"codex\",\"status\":\"'$item_status'\",\"reason\":\"'$reason'\"},"
            "{\"name\":\"codex-code-mode-host\",\"status\":\"'$item_status'\",\"reason\":\"'$reason'\"},"
            "{\"name\":\"agent\",\"status\":\"'$item_status'\",\"reason\":\"'$reason'\"}]}'\n",
            encoding="utf-8",
        )
        factory_kit.chmod(0o755)
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
            ROOT / "scripts/ticket-readiness.py",
            self.factory / "scripts/ticket-readiness.py",
        )
        shutil.copy2(
            ROOT / "scripts/qualification-reducer.py",
            self.factory / "scripts/qualification-reducer.py",
        )
        shutil.copy2(
            ROOT / "scripts/operator-cli.py",
            self.factory / "scripts/operator-cli.py",
        )
        shutil.copy2(
            ROOT / "scripts/ledger-view.py",
            self.factory / "scripts/ledger-view.py",
        )
        shutil.copy2(
            ROOT / "scripts/dispatch-plan.py",
            self.factory / "scripts/dispatch-plan.py",
        )
        (self.factory / "scripts/lib").mkdir()
        shutil.copy2(
            ROOT / "scripts/lib/operator_receipt.py",
            self.factory / "scripts/lib/operator_receipt.py",
        )
        shutil.copy2(
            ROOT / "scripts/lib/qualification_artifacts.py",
            self.factory / "scripts/lib/qualification_artifacts.py",
        )
        shutil.copy2(
            ROOT / "scripts/lib/qualification_manifest.py",
            self.factory / "scripts/lib/qualification_manifest.py",
        )
        shutil.copy2(
            ROOT / "scripts/lib/effective_ticket.py",
            self.factory / "scripts/lib/effective_ticket.py",
        )
        shutil.copy2(
            ROOT / "scripts/lib/legacy_closeout.py",
            self.factory / "scripts/lib/legacy_closeout.py",
        )
        shutil.copy2(
            ROOT / "scripts/lib/approval_evidence.py",
            self.factory / "scripts/lib/approval_evidence.py",
        )
        shutil.copy2(
            ROOT / "scripts/lib/protected_merge_reconciliation.py",
            self.factory / "scripts/lib/protected_merge_reconciliation.py",
        )
        shutil.copy2(
            ROOT / "scripts/lib/terminal_backfill.py",
            self.factory / "scripts/lib/terminal_backfill.py",
        )
        shutil.copy2(
            ROOT / "scripts/lib/dependency_fulfillment.py",
            self.factory / "scripts/lib/dependency_fulfillment.py",
        )
        shutil.copy2(
            ROOT / "scripts/certification-preflight.py",
            self.factory / "scripts/certification-preflight.py",
        )
        shutil.copy2(
            ROOT / "scripts/envelope-control.py",
            self.factory / "scripts/envelope-control.py",
        )
        shutil.copy2(
            ROOT / "scripts/owner-runtime-pin.py",
            self.factory / "scripts/owner-runtime-pin.py",
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
            "GH_REPO=example/product\nPREVIEW_PROVIDER=railway\nTEST_PATHS=tests/\n",
            encoding="utf-8",
        )
        (self.product / "factory/ENVELOPE.env").write_text(
            "PER_RUN_BUDGET_USD=2.000000\n"
            "PER_TICKET_BUDGET_USD=25.000000\n"
            "PER_RUN_MAX_TURNS=60\n"
            "PER_RUN_TIMEOUT_MIN=45\n"
            "DAILY_CAP_USD=100.000000\n",
            encoding="utf-8",
        )
        (self.product / "factory/ledger.csv").write_text(
            "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,"
            "exit_status\n",
            encoding="utf-8",
        )
        (self.product / ".gitignore").write_text(
            "factory/runs/\n"
            "factory/.dispatch-leases/\n"
            "factory/.dispatch-leases.lock/\n"
            "factory/.launch.lock/\n",
            encoding="utf-8",
        )
        (self.product / "factory/tickets").mkdir()
        (self.product / "factory/initiatives").mkdir()
        (self.product / "factory/initiatives/I-001.md").write_text(
            "# I-001\n\nStatus: planned\n", encoding="utf-8",
        )
        builder_paths = {
            "T-101": "app/server.js",
            "T-102": "app/worker.js",
            "T-103": "app/job.js",
        }
        for ticket in ("T-101", "T-102", "T-103"):
            (self.product / f"factory/tickets/{ticket}.md").write_text(
                f"# {ticket}\n\nState: Ready\nProduct-Decisions: frozen\n"
                "Initiative: I-001\nPriority: normal\n"
                "Depends-On: none\nFixture-Seams: none\n"
                "Authentication-Seams: none\nProtected-Test-Conflicts: none\n"
                f"Builder ownership: {builder_paths[ticket]} only\n",
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
        run(
            self.product, "git", "remote", "add", "origin",
            "git@github.com:example/product.git",
        )
        run(self.product, "git", "add", ".")
        run(self.product, "git", "commit", "-qm", "product")
        run(
            self.product, "git", "update-ref", "refs/remotes/origin/main",
            run(self.product, "git", "rev-parse", "HEAD"),
        )
        self.operator_seed = self.workspace / "operator-map-seed.json"
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
        self.branch_preflight = mock.patch.object(
            ENVIRONMENT, "validate_selected_remote_branches",
        )
        self.branch_preflight.start()

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
        if self.branch_preflight is not None:
            self.branch_preflight.stop()
        if self.root.exists():
            for base, directories, files in os.walk(self.root, topdown=False):
                for name in files:
                    path = Path(base) / name
                    if not path.is_symlink():
                        path.chmod(0o600)
                for name in directories:
                    path = Path(base) / name
                    if not path.is_symlink():
                        path.chmod(0o700)
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

    def test_provider_cli_pin_gate_rejects_ambiguous_or_stale_evidence(self) -> None:
        sha = "a" * 40
        items = [
            {"name": name, "status": "ok", "reason": "exact_pin_ready"}
            for name in ("claude", "codex", "codex-code-mode-host", "agent")
        ]
        ready = {
            "schema": "nysa.software-factory.provider-cli-pin-status/v1",
            "status": "ready",
            "requested_release": {"factory_sha": sha},
            "items": items,
        }
        with mock.patch.object(
            ENVIRONMENT.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 0, json.dumps(ready), ""),
        ):
            ENVIRONMENT.validate_provider_cli_pins(self.factory, sha)

        invalid = (
            {**ready, "status": "unready"},
            {**ready, "requested_release": {"factory_sha": "b" * 40}},
            {**ready, "items": items + [items[0]]},
            {**ready, "items": [{**items[0], "reason": "receipt_drift"}, *items[1:]]},
        )
        for value in invalid:
            with self.subTest(value=value), mock.patch.object(
                ENVIRONMENT.subprocess, "run",
                return_value=subprocess.CompletedProcess(
                    [], 0, json.dumps(value), "sensitive provider detail",
                ),
            ), self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError,
                "qualification candidate provider CLI pins are not ready",
            ) as refused:
                ENVIRONMENT.validate_provider_cli_pins(self.factory, sha)
            self.assertNotIn("sensitive provider detail", str(refused.exception))

    def use_real_branch_preflight(self) -> Path:
        self.branch_preflight.stop()
        self.branch_preflight = None
        remote = self.workspace / "product.git"
        run(self.workspace, "git", "init", "--bare", "-q", str(remote))
        run(self.product, "git", "remote", "set-url", "origin", str(remote))
        run(self.product, "git", "push", "-qu", "origin", "main")
        return remote

    def use_contract_2(self) -> None:
        (self.factory / "factory-contract.json").write_text(
            '{"contract_version":"2.0.0"}\n', encoding="utf-8",
        )
        run(self.factory, "git", "add", "factory-contract.json")
        run(self.factory, "git", "commit", "-qm", "use Contract 2")
        self.sha = run(self.factory, "git", "rev-parse", "HEAD")
        (self.product / "factory/KIT_PIN").write_text(self.sha + "\n")
        manifest = json.loads(
            (self.product / "factory/QUALIFICATION.json").read_text()
        )
        manifest["contract_version"] = "2.0.0"
        manifest["factory_sha"] = self.sha
        (self.product / "factory/QUALIFICATION.json").write_text(
            json.dumps(manifest) + "\n"
        )
        descriptor = self.product / "factory/PROJECT.env"
        descriptor.write_text(
            descriptor.read_text() + "MAX_CONCURRENT_TICKETS=3\n"
        )
        run(
            self.product, "git", "add", "factory/KIT_PIN",
            "factory/QUALIFICATION.json", "factory/PROJECT.env",
        )
        run(self.product, "git", "commit", "-qm", "pin Contract 2 candidate")

    def stale_selected_branch(
        self, remote: Path, *, publish_authorization: bool = True,
    ) -> str:
        ticket = "T-101"
        branch = f"ticket/{ticket}"
        run(self.product, "git", "switch", "-qc", branch)
        ticket_path = self.product / f"factory/tickets/{ticket}.md"
        ticket_path.write_text(ticket_path.read_text() + f"Kit-SHA: {self.sha}\n")
        plan = self.product / f"factory/route-plans/{ticket}.json"
        plan.parent.mkdir()
        plan.write_text(json.dumps({
            "kit_sha": self.sha,
            "schema": "ticket-model-route-plan/v1",
            "ticket": ticket,
        }) + "\n")
        run(self.product, "git", "add", str(ticket_path), str(plan))
        run(
            self.product, "git", "-c", "user.name=Software Factory",
            "-c", "user.email=factory@local", "commit", "-qm",
            f"{ticket}: pin kit and model route plan",
        )
        ticket_path.write_text(
            ticket_path.read_text().replace("State: Ready", "State: Planning")
        )
        run(self.product, "git", "add", str(ticket_path))
        run(
            self.product, "git", "-c", "user.name=Software Factory",
            "-c", "user.email=factory@local", "commit", "-qm",
            f"{ticket}: transition ticket state",
        )
        head = run(self.product, "git", "rev-parse", "HEAD")
        run(self.product, "git", "push", "-q", str(remote), branch)
        run(self.product, "git", "switch", "-q", "main")
        authorization = self.product / (
            "factory/qualification/preprovider-branch-resets.json"
        )
        authorization.parent.mkdir(exist_ok=True)
        authorization.write_text(json.dumps({
            "factory_sha": self.sha,
            "resets": [{
                "branch": branch, "head": head, "ticket": ticket,
            }],
            "schema": "nysa.software-factory.preprovider-branch-resets/v1",
        }, sort_keys=True, separators=(",", ":")) + "\n")
        run(self.product, "git", "add", str(authorization))
        run(self.product, "git", "commit", "-qm", "authorize selected branch reset")
        if publish_authorization:
            run(self.product, "git", "push", "-q", str(remote), "main")
        return head

    def stale_operator_ready_selected_branch(self, remote: Path) -> str:
        ticket = "T-101"
        ticket_path = self.product / f"factory/tickets/{ticket}.md"
        ticket_path.write_text(
            ticket_path.read_text().replace("State: Ready", "State: Backlog")
        )
        run(self.product, "git", "add", str(ticket_path))
        run(self.product, "git", "commit", "-qm", "protect backlog ticket")
        run(self.product, "git", "push", "-q", str(remote), "main")
        branch = f"ticket/{ticket}"
        run(self.product, "git", "switch", "-qc", branch)
        receipt = self.product / f"factory/receipts/{ticket}/ready-1.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({
            "action": "ready", "audit": "no-authority", "consumed": False,
            "issued_at": "2026-01-01T00:00:00Z", "payload": {},
            "receipt_sha256": "c" * 64,
            "schema": "nysa.software-factory.operator-receipt/v1",
            "sequence": 1, "ticket": ticket,
        }) + "\n")
        run(self.product, "git", "add", str(receipt))
        run(
            self.product, "git", "-c", "user.name=Factory Operator",
            "-c", "user.email=operator@local", "commit", "-qm",
            f"{ticket}: operator ready receipt 1",
        )
        ticket_path.write_text(
            ticket_path.read_text().replace("State: Backlog", "State: Ready")
        )
        run(self.product, "git", "add", str(ticket_path))
        run(
            self.product, "git", "-c", "user.name=Software Factory",
            "-c", "user.email=factory@local", "commit", "-qm",
            f"{ticket}: materialize ticket state",
        )
        head = run(self.product, "git", "rev-parse", "HEAD")
        run(self.product, "git", "push", "-q", str(remote), branch)
        run(self.product, "git", "switch", "-q", "main")
        authorization = self.product / (
            "factory/qualification/preprovider-branch-resets.json"
        )
        authorization.parent.mkdir(exist_ok=True)
        authorization.write_text(json.dumps({
            "factory_sha": self.sha,
            "resets": [{
                "branch": branch, "head": head, "ticket": ticket,
            }],
            "schema": "nysa.software-factory.preprovider-branch-resets/v1",
        }, sort_keys=True, separators=(",", ":")) + "\n")
        run(self.product, "git", "add", str(authorization))
        run(self.product, "git", "commit", "-qm", "authorize operator branch reset")
        run(self.product, "git", "push", "-q", str(remote), "main")
        return head

    def qualification_control_selected_branch(self, remote: Path) -> str:
        self.use_contract_2()
        ready = self.stale_operator_ready_selected_branch(remote)
        source_product_sha = run(
            self.product, "git", "merge-base", "HEAD", ready,
        )
        ticket = "T-101"
        ticket_path = self.product / f"factory/tickets/{ticket}.md"
        run(self.product, "git", "switch", "-q", f"ticket/{ticket}")
        ticket_path.write_text(ticket_path.read_text() + f"Kit-SHA: {self.sha}\n")
        plan = self.product / f"factory/route-plans/{ticket}.json"
        plan.parent.mkdir(exist_ok=True)
        plan.write_text(json.dumps({
            "kit_sha": self.sha,
            "schema": "ticket-model-route-plan/v1",
            "ticket": ticket,
        }) + "\n")
        run(self.product, "git", "add", str(ticket_path), str(plan))
        run(
            self.product, "git", "-c", "user.name=Software Factory",
            "-c", "user.email=factory@local", "commit", "-qm",
            f"{ticket}: pin kit and model route plan",
        )
        ticket_path.write_text(
            ticket_path.read_text().replace("State: Ready", "State: Planning")
        )
        run(self.product, "git", "add", str(ticket_path))
        run(self.product, "git", "commit", "-qm", f"{ticket}: plan qualification")
        head = run(self.product, "git", "rev-parse", "HEAD")
        run(self.product, "git", "push", "-q", str(remote), f"ticket/{ticket}")
        run(self.product, "git", "switch", "-q", "main")

        manifest_path = self.product / "factory/QUALIFICATION.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["generation"] = 2
        manifest_path.write_text(json.dumps(manifest) + "\n")
        authorization = self.product / (
            "factory/qualification/preprovider-branch-resets.json"
        )
        authorization.write_text(json.dumps({
            "factory_sha": self.sha,
            "resets": [{
                "branch": f"ticket/{ticket}", "head": head, "ticket": ticket,
            }],
            "schema": "nysa.software-factory.preprovider-branch-resets/v2",
            "source_qualification": {
                "factory_sha": self.sha,
                "generation": 1,
                "product_sha": source_product_sha,
            },
        }, sort_keys=True, separators=(",", ":")) + "\n")
        run(self.product, "git", "add", str(manifest_path), str(authorization))
        run(self.product, "git", "commit", "-qm", "authorize qualification retry")
        run(self.product, "git", "push", "-q", str(remote), "main")
        return head

    def test_prepares_exact_read_only_candidate_once(self) -> None:
        args = argparse.Namespace(
            factory_root=self.factory,
            product_root=self.product,
            project="relay",
            root=self.root,
        )
        value = ENVIRONMENT.prepare(args)
        release = Path(value["launcher"]).parent.parent
        authority = Path(value["authority_root"])
        self.assertEqual(value["factory_sha"], self.sha)
        lane = ENVIRONMENT.qualification_lane(self.root, "relay")
        self.assertEqual(lane["active"]["kit_sha"], self.sha)
        active = ENVIRONMENT.read(self.root / "projects/relay/active.json")
        receipt = ENVIRONMENT.read(
            self.root / "receipts" / f"{active['receipt_id']}.json"
        )
        operator_map = authority / "operator/operator-map.json"
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
        for relative in ("operator-map.json", "runtime-ledger.csv"):
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
            {path.name for path in self.root.iterdir()},
            {
                "environment.json", "global.env", "marker.json", "projects",
                "project-runtimes", "receipts", "releases",
            },
        )
        runtime = self.root / "project-runtimes/relay"
        self.assertTrue((runtime / "runtime-pin-journal.json").is_file())
        for tool in ("node", "npm", "npx"):
            self.assertTrue((runtime / "bin" / tool).is_symlink())
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
            ROOT / "scripts/factory-launch"
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
                ENVIRONMENT, "initialize_selected_operator",
                side_effect=AssertionError(
                    "complete replay must not initialize operator state"
                ),
            ),
            mock.patch.object(
                ENVIRONMENT, "qualification_fallback_readiness",
                wraps=ENVIRONMENT.qualification_fallback_readiness,
            ) as readiness,
        ):
            replay = ENVIRONMENT.prepare(args)
        self.assertEqual(replay, value)
        readiness.assert_called_once()

    def test_sealed_qualification_resume_is_isolated_and_exact(self) -> None:
        self.use_contract_2()
        marker = self.workspace / "qualification-resume-args.json"
        history_marker = self.workspace / "qualification-history-args.json"
        shutil.copy2(
            ROOT / "scripts/factory-launch",
            self.factory / "scripts/factory-launch",
        )
        shutil.copy2(
            ROOT / "factory-contract.json", self.factory / "factory-contract.json",
        )
        runner = self.factory / "scripts/qualification-run.py"
        runner.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            f"pathlib.Path({str(marker)!r}).write_text(json.dumps(sys.argv[1:]))\n"
            "print('{\"schema\":\"nysa.software-factory.qualification-run/v1\",'"
            "'\"status\":\"projected\"}')\n",
            encoding="utf-8",
        )
        runner.chmod(0o755)
        controller = self.factory / "scripts/factory-controller.py"
        controller.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            f"pathlib.Path({str(history_marker)!r}).write_text(json.dumps(sys.argv[1:]))\n"
            "print('{\"schema\":\"nysa.software-factory.controller/v1\",'"
            "'\"status\":\"repaired\"}')\n",
            encoding="utf-8",
        )
        controller.chmod(0o755)
        run(self.factory, "git", "add", ".")
        run(self.factory, "git", "commit", "-qm", "seal launcher fixture")
        self.sha = run(self.factory, "git", "rev-parse", "HEAD")
        (self.product / "factory/KIT_PIN").write_text(self.sha + "\n")
        manifest = json.loads(
            (self.product / "factory/QUALIFICATION.json").read_text()
        )
        manifest["factory_sha"] = self.sha
        (self.product / "factory/QUALIFICATION.json").write_text(
            json.dumps(manifest) + "\n"
        )
        run(
            self.product, "git", "add", "factory/KIT_PIN",
            "factory/QUALIFICATION.json",
        )
        run(self.product, "git", "commit", "-qm", "pin launcher fixture")

        project = f"qualification-launcher-{os.getpid()}-{self.root.name[-6:]}"
        value = ENVIRONMENT.prepare(argparse.Namespace(
            factory_root=self.factory, product_root=self.product,
            project=project, root=self.root,
        ))
        active_path = self.root / f"projects/{project}/active.json"
        active = ENVIRONMENT.read(active_path)
        receipt_path = self.root / f"receipts/{active['receipt_id']}.json"
        receipt = ENVIRONMENT.read(receipt_path)
        account_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        authority = account_home / f".factory/qualification/{project}"
        self.assertFalse(authority.exists())
        authority.parent.mkdir(parents=True, exist_ok=True)
        takeover_kits_root = account_home / ".factory/kits"
        created_takeover_kits_root = not takeover_kits_root.exists()
        if created_takeover_kits_root:
            takeover_kits_root.mkdir(mode=0o700)
        shutil.copytree(Path(value["authority_root"]), authority)
        isolated_paths = {
            "controller_state_path": str(authority / "controller"),
            "operator_map_path": str(authority / "operator/operator-map.json"),
            "provider_state_path": str(authority / "provider"),
            "runtime_ledger_path": str(authority / "operator/runtime-ledger.csv"),
        }

        def replace_json(path: Path, value: dict[str, object]) -> None:
            temporary = path.with_name(path.name + ".test-tmp")
            ENVIRONMENT.write(temporary, value)
            os.replace(temporary, path)

        def write_mode(mode: str) -> None:
            selected_active = {**active, **isolated_paths, "qualification_mode": mode}
            selected_receipt = {
                **receipt,
                "operator_map_path": isolated_paths["operator_map_path"],
                "qualification_mode": mode,
            }
            if mode == "isolated":
                selected_active.pop("takeover_kits_root", None)
                selected_receipt.pop("takeover_kits_root", None)
                selected_receipt["runtime_ledger_path"] = isolated_paths[
                    "runtime_ledger_path"
                ]
            else:
                takeover = str(takeover_kits_root)
                selected_active["takeover_kits_root"] = takeover
                selected_active.pop("runtime_ledger_path", None)
                selected_receipt["takeover_kits_root"] = takeover
                selected_receipt.pop("runtime_ledger_path", None)
            replace_json(active_path, selected_active)
            replace_json(receipt_path, selected_receipt)

        def snapshot() -> tuple[
            str, str, bytes, bytes, list[tuple[str, bytes]],
        ]:
            return (
                run(self.product, "git", "rev-parse", "HEAD"),
                run(self.product, "git", "status", "--porcelain"),
                active_path.read_bytes(),
                receipt_path.read_bytes(),
                [
                    (str(path.relative_to(authority)), path.read_bytes())
                    for path in sorted(authority.rglob("*")) if path.is_file()
                ],
            )

        launcher = Path(value["launcher"])
        receipt_digest = "a" * 64
        try:
            write_mode("isolated")
            before = snapshot()
            result = subprocess.run([
                str(launcher), project, "qualification-resume",
                "--ticket", "T-101", "--blocked-receipt", receipt_digest,
                "--json",
            ], capture_output=True, check=False, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(marker.read_text()), [
                "--launcher", str(launcher), "--project", project,
                "--resume-ticket", "T-101", "--resume-receipt",
                receipt_digest, "--json",
            ])
            self.assertEqual(snapshot(), before)

            result = subprocess.run([
                str(launcher), project, "qualification-history-repair",
                "--ticket", "T-101", "--blocked-receipt", receipt_digest,
                "--json",
            ], capture_output=True, check=False, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            history_args = json.loads(history_marker.read_text())
            self.assertEqual(history_args, [
                "--launcher", str(launcher), "--project", project,
                "--product-root", str(self.product.resolve()),
                "--release-path", str(launcher.parent.parent),
                "--state-dir", str(authority / "controller"),
                "--action", "qualification-history-repair",
                "--ticket", "T-101", "--receipt", receipt_digest,
            ])
            self.assertEqual(snapshot(), before)

            for arguments in (
                ("--ticket", "bad", "--blocked-receipt", receipt_digest, "--json"),
                ("--blocked-receipt", receipt_digest, "--ticket", "T-101", "--json"),
                ("--ticket", "T-101", "--blocked-receipt", "bad", "--json"),
                ("--ticket", "T-101", "--blocked-receipt", receipt_digest),
            ):
                marker.unlink(missing_ok=True)
                result = subprocess.run(
                    [str(launcher), project, "qualification-resume", *arguments],
                    capture_output=True, check=False, text=True, timeout=120,
                )
                self.assertEqual(result.returncode, 2)
                self.assertFalse(marker.exists())
                self.assertEqual(snapshot(), before)

            for arguments in (
                ("--ticket", "bad", "--blocked-receipt", receipt_digest, "--json"),
                ("--blocked-receipt", receipt_digest, "--ticket", "T-101", "--json"),
                ("--ticket", "T-101", "--blocked-receipt", "bad", "--json"),
                ("--ticket", "T-101", "--blocked-receipt", receipt_digest),
            ):
                history_marker.unlink(missing_ok=True)
                result = subprocess.run([
                    str(launcher), project, "qualification-history-repair",
                    *arguments,
                ], capture_output=True, check=False, text=True, timeout=120)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(history_marker.exists())
                self.assertEqual(snapshot(), before)

            write_mode("takeover")
            marker.unlink(missing_ok=True)
            before = snapshot()
            result = subprocess.run([
                str(launcher), project, "qualification-resume",
                "--ticket", "T-101", "--blocked-receipt", receipt_digest,
                "--json",
            ], capture_output=True, check=False, text=True, timeout=120)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "qualification resume requires a sealed isolated qualification launcher",
                result.stderr,
            )
            self.assertFalse(marker.exists())
            self.assertEqual(snapshot(), before)
            history_marker.unlink(missing_ok=True)
            result = subprocess.run([
                str(launcher), project, "qualification-history-repair",
                "--ticket", "T-101", "--blocked-receipt", receipt_digest,
                "--json",
            ], capture_output=True, check=False, text=True, timeout=120)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "qualification history repair requires a sealed isolated qualification launcher",
                result.stderr,
            )
            self.assertFalse(history_marker.exists())
            self.assertEqual(snapshot(), before)
        finally:
            shutil.rmtree(authority, ignore_errors=True)
            if created_takeover_kits_root:
                takeover_kits_root.rmdir()

    def test_prepare_rejects_unfit_run_budget_before_state(self) -> None:
        envelope = self.product / "factory/ENVELOPE.env"
        envelope.write_text(
            envelope.read_text(encoding="utf-8").replace(
                "PER_RUN_BUDGET_USD=2.000000",
                "PER_RUN_BUDGET_USD=10.000000",
            ),
            encoding="utf-8",
        )
        run(self.product, "git", "add", "factory/ENVELOPE.env")
        run(self.product, "git", "commit", "-qm", "oversized qualification run")
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError,
            "qualification product per-run budget exceeds the manifest",
        ):
            ENVIRONMENT.prepare(argparse.Namespace(
                factory_root=self.factory, product_root=self.product,
                project="relay", root=self.root,
            ))
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertFalse((self.product / "factory/runs").exists())
        self.assertFalse(self.home.joinpath(".factory/qualification/relay").exists())

    def test_prepare_validates_exact_selected_branch_before_publication(self) -> None:
        remote = self.use_real_branch_preflight()
        expected = self.stale_selected_branch(remote)
        args = argparse.Namespace(
            factory_root=self.factory, product_root=self.product,
            project="relay", root=self.root,
        )
        with mock.patch.object(
            ENVIRONMENT, "qualification_publication_origin",
        ):
            value = ENVIRONMENT.prepare(args)
        self.assertEqual(value["status"], "prepared")
        self.assertEqual(
            run(
                self.product, "git", "ls-remote", "--heads", str(remote),
                "refs/heads/ticket/T-101",
            ).split()[0],
            expected,
        )
        run(
            self.product, "git", "push", "-q", str(remote),
            "+main:refs/heads/ticket/T-101",
        )
        with mock.patch.object(
            ENVIRONMENT, "qualification_publication_origin",
        ):
            self.assertEqual(ENVIRONMENT.prepare(args), value)

    def test_prepare_does_not_advance_authorized_operator_ready_branch(self) -> None:
        self.use_contract_2()
        remote = self.use_real_branch_preflight()
        expected = self.stale_operator_ready_selected_branch(remote)
        args = argparse.Namespace(
            factory_root=self.factory, product_root=self.product,
            project="relay", root=self.root,
        )
        with mock.patch.object(
            ENVIRONMENT, "qualification_publication_origin",
        ):
            value = ENVIRONMENT.prepare(args)
        self.assertEqual(value["status"], "prepared")
        self.assertEqual(
            run(
                self.product, "git", "ls-remote", "--heads", str(remote),
                "refs/heads/ticket/T-101",
            ).split()[0],
            expected,
        )
        active = ENVIRONMENT.read(self.root / "projects/relay/active.json")
        worktrees = self.root / "worktrees"
        worktrees.mkdir(mode=0o700)
        result = subprocess.run(
            [
                sys.executable, str(ROOT / "scripts/dispatch-plan.py"),
                "--factory-root", str(self.product.resolve()),
                "--worktree-root", str(worktrees.resolve()), "claim",
            ],
            text=True, capture_output=True, check=False, timeout=60,
            env={
                **os.environ,
                "FACTORY_CERTIFIED_PRODUCT_ORIGIN": str(remote),
                "FACTORY_CONTROLLER_STATE_DIR": active["controller_state_path"],
                "FACTORY_OPERATOR_MAP": active["operator_map_path"],
                "FACTORY_RELEASE_CONTRACT_VERSION": "2.0.0",
            },
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        claim = json.loads(result.stdout)
        self.assertIn("ticket", claim, claim)
        self.assertEqual(claim["ticket"], "T-101")
        self.assertEqual(claim["preprovider_reset_head"], expected)
        self.assertIn(
            "State: Ready",
            Path(claim["worktree"])
            .joinpath("factory/tickets/T-101.md").read_text(),
        )

    def test_prepare_accepts_authenticated_qualification_control_retry(self) -> None:
        remote = self.use_real_branch_preflight()
        expected = self.qualification_control_selected_branch(remote)
        args = argparse.Namespace(
            factory_root=self.factory, product_root=self.product,
            project="relay", root=self.root,
        )
        with mock.patch.object(ENVIRONMENT, "qualification_publication_origin"):
            value = ENVIRONMENT.prepare(args)
        self.assertEqual(value["status"], "prepared")
        self.assertEqual(
            run(
                self.product, "git", "ls-remote", "--heads", str(remote),
                "refs/heads/ticket/T-101",
            ).split()[0],
            expected,
        )
        active = ENVIRONMENT.read(self.root / "projects/relay/active.json")
        worktrees = self.root / "worktrees"
        worktrees.mkdir(mode=0o700)
        result = subprocess.run(
            [
                sys.executable, str(ROOT / "scripts/dispatch-plan.py"),
                "--factory-root", str(self.product.resolve()),
                "--worktree-root", str(worktrees.resolve()), "claim",
            ],
            text=True, capture_output=True, check=False, timeout=60,
            env={
                **os.environ,
                "FACTORY_CERTIFIED_PRODUCT_ORIGIN": str(remote),
                "FACTORY_CONTROLLER_STATE_DIR": active["controller_state_path"],
                "FACTORY_OPERATOR_MAP": active["operator_map_path"],
                "FACTORY_RELEASE_CONTRACT_VERSION": "2.0.0",
            },
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        claim = json.loads(result.stdout)
        self.assertEqual(claim["ticket"], "T-101")
        self.assertEqual(claim["preprovider_reset_head"], expected)
        self.assertIn(
            "State: Ready",
            Path(claim["worktree"])
            .joinpath("factory/tickets/T-101.md").read_text(),
        )

    def test_fresh_prepare_and_claim_accept_prior_canonical_ready_reset(self) -> None:
        self.use_contract_2()
        remote = self.use_real_branch_preflight()
        self.stale_operator_ready_selected_branch(remote)
        args = argparse.Namespace(
            factory_root=self.factory, product_root=self.product,
            project="relay", root=self.root,
        )
        with mock.patch.object(ENVIRONMENT, "qualification_publication_origin"):
            ENVIRONMENT.prepare(args)
        active = ENVIRONMENT.read(self.root / "projects/relay/active.json")
        worktrees = self.root / "worktrees"
        worktrees.mkdir(mode=0o700)
        environment = {
            **os.environ,
            "FACTORY_CERTIFIED_PRODUCT_ORIGIN": str(remote),
            "FACTORY_CONTROLLER_STATE_DIR": active["controller_state_path"],
            "FACTORY_OPERATOR_MAP": active["operator_map_path"],
            "FACTORY_RELEASE_CONTRACT_VERSION": "2.0.0",
        }
        first = subprocess.run(
            [
                sys.executable, str(ROOT / "scripts/dispatch-plan.py"),
                "--factory-root", str(self.product.resolve()),
                "--worktree-root", str(worktrees.resolve()), "claim",
            ],
            text=True, capture_output=True, check=True, timeout=60,
            env=environment,
        )
        first_claim = json.loads(first.stdout)
        first_ready = run(
            self.product, "git", "ls-remote", "--heads", str(remote),
            "refs/heads/ticket/T-101",
        ).split()[0]
        self.product.joinpath(
            "factory/.dispatch-leases/T-101.json"
        ).unlink()
        run(
            self.product, "git", "worktree", "remove", first_claim["worktree"],
        )
        run(self.product, "git", "branch", "-D", "ticket/T-101")

        manifest_path = self.product / "factory/QUALIFICATION.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["generation"] = 2
        manifest_path.write_text(json.dumps(manifest) + "\n")
        authorization = self.product / (
            "factory/qualification/preprovider-branch-resets.json"
        )
        value = json.loads(authorization.read_text())
        value["resets"][0]["head"] = first_ready
        authorization.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        )
        run(self.product, "git", "add", str(manifest_path), str(authorization))
        run(self.product, "git", "commit", "-qm", "prepare qualification generation 2")
        run(self.product, "git", "push", "-q", str(remote), "main")

        second_root = Path(tempfile.mkdtemp(
            prefix="nysa-sf-qualification.x-", dir="/private/tmp",
        )).resolve()
        os.chmod(second_root, 0o700)
        self.addCleanup(shutil.rmtree, second_root, ignore_errors=True)
        second_args = argparse.Namespace(
            factory_root=self.factory, product_root=self.product,
            project="r", root=second_root,
        )
        with mock.patch.object(ENVIRONMENT, "qualification_publication_origin"):
            prepared = ENVIRONMENT.prepare(second_args)
        self.assertEqual(prepared["status"], "prepared")
        second_active = ENVIRONMENT.read(
            second_root / "projects/r/active.json"
        )
        second_worktrees = second_root / "worktrees"
        second_worktrees.mkdir(mode=0o700)
        second = subprocess.run(
            [
                sys.executable, str(ROOT / "scripts/dispatch-plan.py"),
                "--factory-root", str(self.product.resolve()),
                "--worktree-root", str(second_worktrees.resolve()), "claim",
            ],
            text=True, capture_output=True, check=True, timeout=60,
            env={
                **os.environ,
                "FACTORY_CERTIFIED_PRODUCT_ORIGIN": str(remote),
                "FACTORY_CONTROLLER_STATE_DIR": second_active[
                    "controller_state_path"
                ],
                "FACTORY_OPERATOR_MAP": second_active["operator_map_path"],
                "FACTORY_RELEASE_CONTRACT_VERSION": "2.0.0",
            },
        )
        second_claim = json.loads(second.stdout)
        self.assertEqual(second_claim["ticket"], "T-101")
        self.assertEqual(second_claim["preprovider_reset_head"], first_ready)
        self.assertIn(
            "State: Ready",
            Path(second_claim["worktree"])
            .joinpath("factory/tickets/T-101.md").read_text(),
        )

    def test_prepare_refuses_selected_branch_head_drift_before_state(self) -> None:
        remote = self.use_real_branch_preflight()
        authorized = self.stale_selected_branch(remote)
        run(self.product, "git", "switch", "-q", "ticket/T-101")
        run(
            self.product, "git", "commit", "--allow-empty", "-qm",
            "advance selected branch",
        )
        advanced = run(self.product, "git", "rev-parse", "HEAD")
        run(self.product, "git", "push", "-q", str(remote), "ticket/T-101")
        run(self.product, "git", "switch", "-q", "main")
        args = argparse.Namespace(
            factory_root=self.factory, product_root=self.product,
            project="relay", root=self.root,
        )
        with (
            mock.patch.object(ENVIRONMENT, "qualification_publication_origin"),
            mock.patch.object(
                ENVIRONMENT, "initialize_selected_operator",
                side_effect=AssertionError("operator state must not be initialized"),
            ),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError,
                "ticket remote branch does not match reset authorization",
            ),
        ):
            ENVIRONMENT.prepare(args)
        self.assertNotEqual(advanced, authorized)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertFalse((self.product / "factory/runs").exists())
        self.assertFalse(
            self.home.joinpath(".factory/qualification/relay").exists()
        )

    def test_prepare_refuses_local_only_reset_authorization_before_state(self) -> None:
        remote = self.use_real_branch_preflight()
        selected_head = self.stale_selected_branch(
            remote, publish_authorization=False,
        )
        args = argparse.Namespace(
            factory_root=self.factory, product_root=self.product,
            project="relay", root=self.root,
        )
        with (
            mock.patch.object(ENVIRONMENT, "qualification_publication_origin"),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError,
                "reset authorization is not protected",
            ),
        ):
            ENVIRONMENT.prepare(args)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertFalse((self.product / "factory/runs").exists())
        self.assertEqual(
            run(
                self.product, "git", "ls-remote", "--heads", str(remote),
                "refs/heads/ticket/T-101",
            ).split()[0],
            selected_head,
        )

    def test_qualification_budget_contract_covers_every_cap(self) -> None:
        manifest = json.loads(
            (self.product / "factory/QUALIFICATION.json").read_text(encoding="utf-8")
        )
        envelope = self.product / "factory/ENVELOPE.env"
        original = envelope.read_text(encoding="utf-8")
        cases = (
            (
                original + "PLANNER_PER_RUN_BUDGET_USD=2.000001\n",
                self.global_env.read_bytes(),
                "qualification product per-run budget exceeds the manifest",
            ),
            (
                original.replace(
                    "PER_TICKET_BUDGET_USD=25.000000",
                    "PER_TICKET_BUDGET_USD=26.000000",
                ),
                self.global_env.read_bytes(),
                "qualification product ticket budget does not match the manifest",
            ),
            (
                original.replace(
                    "DAILY_CAP_USD=100.000000", "DAILY_CAP_USD=99.999999",
                ),
                self.global_env.read_bytes(),
                "qualification product daily cap is below the manifest budget",
            ),
            (
                original,
                b"GLOBAL_DAILY_CAP_USD=99.999999\n",
                "qualification machine daily cap is below the manifest budget",
            ),
        )
        for env_text, global_config, message in cases:
            with self.subTest(message=message):
                envelope.write_text(env_text, encoding="utf-8")
                with self.assertRaisesRegex(ENVIRONMENT.EnvironmentError, message):
                    ENVIRONMENT.validate_qualification_budget(
                        self.factory, self.product, manifest, global_config,
                    )

        manifest.update({
            "budget_usd": "300.000000",
            "per_run_budget_usd": "10.000000",
            "per_ticket_budget_usd": "100.000000",
        })
        envelope.write_text(
            original.replace(
                "PER_RUN_BUDGET_USD=2.000000", "PER_RUN_BUDGET_USD=10.000000",
            ).replace(
                "PER_TICKET_BUDGET_USD=25.000000",
                "PER_TICKET_BUDGET_USD=100.000000",
            ).replace(
                "DAILY_CAP_USD=100.000000", "DAILY_CAP_USD=300.000000",
            ),
            encoding="utf-8",
        )
        ENVIRONMENT.validate_qualification_budget(
            self.factory, self.product, manifest,
            b"GLOBAL_DAILY_CAP_USD=300.000000\n",
        )
        envelope.write_text(original, encoding="utf-8")

    def test_prepare_recovers_each_exact_crash_prefix_and_response_loss(self) -> None:
        args = argparse.Namespace(
            factory_root=self.factory, product_root=self.product,
            project="relay", root=self.root,
        )
        original_json = ENVIRONMENT.write_exact
        original_bytes = ENVIRONMENT.write_bytes_exact
        authority = self.home / ".factory/qualification/relay"
        self.assertEqual(
            ENVIRONMENT.preparation_state(self.root, authority, "relay"), "fresh",
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
            lambda path: path.name == "environment.json",
            lambda path: path.name == "active.json",
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
                    ENVIRONMENT.preparation_state(self.root, authority, "relay"),
                    "exact-incomplete",
                )

        value = ENVIRONMENT.prepare(args)
        self.assertEqual(value["status"], "prepared")
        self.assertEqual(
            ENVIRONMENT.preparation_state(
                self.root, Path(value["authority_root"]), "relay",
            ),
            "exact-complete",
        )

    def test_prepare_replays_consumed_ready_receipts_after_later_failure(self) -> None:
        args = argparse.Namespace(
            factory_root=self.factory, product_root=self.product,
            project="relay", root=self.root,
        )
        controller = (
            self.home / ".factory/qualification/relay/controller"
        ).resolve()

        def fail_after_operator_initialization(*_arguments):
            lock = os.open(
                controller / ".operator-apply-lock",
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.close(lock)
            for ticket in ("T-101", "T-102", "T-103"):
                ENVIRONMENT.operator_receipt.issue(
                    controller, ticket, "ready", {},
                )
                ENVIRONMENT.operator_receipt.verify_consume(
                    controller, ticket, "ready", {},
                )
            raise ENVIRONMENT.EnvironmentError("simulated later failure")

        with (
            mock.patch.object(
                ENVIRONMENT, "qualification_fallback_readiness",
                side_effect=fail_after_operator_initialization,
            ),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "simulated later failure",
            ),
        ):
            ENVIRONMENT.prepare(args)

        value = ENVIRONMENT.prepare(args)
        self.assertEqual(value["status"], "prepared")
        self.assertEqual(
            ENVIRONMENT.preparation_state(
                self.root, Path(value["authority_root"]), "relay",
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
                ENVIRONMENT, "initialize_selected_operator",
                side_effect=AssertionError(
                    "refusal must precede operator initialization"
                ),
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
                ENVIRONMENT, "initialize_selected_operator",
                side_effect=AssertionError(
                    "refusal must precede operator initialization"
                ),
            ),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "controller is active",
            ),
        ):
            ENVIRONMENT.prepare(args)
        self.assertTrue(active.is_file())
        active.unlink()

        missing = self.root / "projects/relay"
        missing.rmdir()
        with (
            mock.patch.object(
                ENVIRONMENT, "initialize_selected_operator",
                side_effect=AssertionError(
                    "refusal must precede operator initialization"
                ),
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
                ENVIRONMENT, "initialize_selected_operator",
                side_effect=AssertionError(
                    "refusal must precede operator initialization"
                ),
            ),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "preparation artifact changed",
            ),
        ):
            ENVIRONMENT.prepare(args)
        self.assertEqual(snapshot.read_bytes(), b"CHANGED=true\n")

    def test_prepare_refuses_provider_gap_before_operator_init_or_repair(self) -> None:
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
                ENVIRONMENT, "initialize_selected_operator",
                side_effect=AssertionError(
                    "refusal must precede operator initialization"
                ),
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
        foreign_map = foreign / "operator-map.json"
        foreign_ledger = foreign / "runtime-ledger.csv"
        shutil.copyfile(authority / "operator/operator-map.json", foreign_map)
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
            ENVIRONMENT.EnvironmentError, "operator map is malformed",
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

    def test_local_publication_origin_fails_before_materialization(self) -> None:
        remote = self.workspace / "local-only.git"
        run(self.workspace, "git", "init", "--bare", "-q", str(remote))
        run(self.product, "git", "remote", "set-url", "origin", str(remote))
        args = argparse.Namespace(
            factory_root=self.factory, product_root=self.product,
            project="relay", root=self.root,
        )
        with (
            mock.patch.object(
                ENVIRONMENT, "prepare_global_config",
                side_effect=AssertionError("origin refusal must be read-only"),
            ),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError,
                "qualification requires the protected GitHub product origin",
            ),
        ):
            ENVIRONMENT.prepare(args)
        self.assertFalse((self.root / "marker.json").exists())
        self.assertFalse((self.root / "global.env").exists())
        self.assertFalse(
            self.home.joinpath(".factory/qualification/relay").exists()
        )

    def test_partial_selected_initialization_restarts_without_duplication(self) -> None:
        args = argparse.Namespace(
            factory_root=self.factory, product_root=self.product,
            project="relay", root=self.root,
        )
        authority = self.home / ".factory/qualification/relay"

        def interrupt(_factory, _product, map_path, _ledger_path, _state_dir):
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
                ENVIRONMENT, "initialize_selected_operator", side_effect=interrupt,
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
            ENVIRONMENT.read(authority / "operator/operator-map.json")["tickets"]
            ["T-101"]["issue_id"],
            "issue-T-101",
        )

        self.operator_seed.unlink()
        value = ENVIRONMENT.prepare(args)
        mapping = ENVIRONMENT.read(authority / "operator/operator-map.json")
        self.assertEqual(value["status"], "prepared")
        self.assertEqual(mapping["tickets"]["T-101"]["issue_id"], "issue-T-101")
        self.assertEqual(len(mapping["tickets"]), 3)
        self.assertEqual(run(self.product, "git", "status", "--porcelain"), "")

    def test_unconsumed_ready_receipt_reaches_materialization_replay(self) -> None:
        args = argparse.Namespace(
            factory_root=self.factory, product_root=self.product,
            project="relay", root=self.root,
        )

        def interrupt(_factory, _product, map_path, _ledger_path, state_dir):
            receipt = ENVIRONMENT.operator_receipt.issue(
                state_dir, "T-101", "ready", {},
            )
            mapping = ENVIRONMENT.read(map_path)
            mapping["tickets"]["T-101"] = {
                "operator_fields_initialized": True,
                "operator": {
                    "observed_at": receipt["issued_at"],
                    "receipt_sha256": receipt["receipt_sha256"],
                    "state": "Ready",
                    "state_base": "backlog",
                },
            }
            mapping["_sync"]["selected_ticket_success_at"] = {
                "T-101": "2026-08-18T00:00:00Z",
            }
            ENVIRONMENT.replace(map_path, mapping)
            raise ENVIRONMENT.EnvironmentError("simulated materialization failure")

        with (
            mock.patch.object(
                ENVIRONMENT, "initialize_selected_operator", side_effect=interrupt,
            ),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "simulated materialization failure",
            ),
        ):
            ENVIRONMENT.prepare(args)

        map_path = (
            self.home
            / ".factory/qualification/relay/operator/operator-map.json"
        )
        exact = ENVIRONMENT.read(map_path)
        changed = json.loads(json.dumps(exact))
        changed["tickets"]["T-101"]["operator"]["receipt_sha256"] = "f" * 64
        ENVIRONMENT.replace(map_path, changed)
        with (
            mock.patch.object(
                ENVIRONMENT, "initialize_selected_operator",
                side_effect=AssertionError("changed projection must not replay"),
            ),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "qualification controller is active",
            ),
        ):
            ENVIRONMENT.prepare(args)
        ENVIRONMENT.replace(map_path, exact)

        with (
            mock.patch.object(
                ENVIRONMENT, "initialize_selected_operator",
                side_effect=ENVIRONMENT.EnvironmentError(
                    "operator materialization replay reached"
                ),
            ),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError,
                "operator materialization replay reached",
            ),
        ):
            ENVIRONMENT.prepare(args)

    def test_partial_bootstrap_ignores_later_seed_change(self) -> None:
        args = argparse.Namespace(
            factory_root=self.factory, product_root=self.product,
            project="relay", root=self.root,
        )

        def interrupt(_factory, _product, map_path, _ledger_path, _state_dir):
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
                ENVIRONMENT, "initialize_selected_operator", side_effect=interrupt,
            ),
            self.assertRaisesRegex(ENVIRONMENT.EnvironmentError, "interruption"),
        ):
            ENVIRONMENT.prepare(args)
        changed = ENVIRONMENT.read(self.operator_seed)
        changed["_sync"]["last_success_at"] = "2026-08-07T01:00:00+00:00"
        ENVIRONMENT.replace(self.operator_seed, changed)

        ENVIRONMENT.prepare(args)
        lane_map = ENVIRONMENT.read(
            self.home / ".factory/qualification/relay/operator/operator-map.json"
        )
        self.assertNotEqual(
            lane_map["_sync"].get("last_success_at"),
            "2026-08-07T01:00:00+00:00",
        )
        self.assertEqual(lane_map["tickets"]["T-101"]["issue_id"], "issue-T-101")

    def test_second_operator_cycle_remains_outside_product(self) -> None:
        value = ENVIRONMENT.prepare(argparse.Namespace(
            factory_root=self.factory, product_root=self.product,
            project="relay", root=self.root,
        ))
        operator = Path(value["authority_root"]) / "operator"
        mapping = ENVIRONMENT.read(operator / "operator-map.json")
        mapping["_sync"]["last_success_at"] = "2026-08-07T00:01:00+00:00"
        ENVIRONMENT.replace(operator / "operator-map.json", mapping)
        self.assertTrue((operator / "runtime-ledger.csv").is_file())
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

    def test_rejects_external_dependency_without_terminal_evidence(self) -> None:
        dependency = self.product / "factory/tickets/T-099.md"
        dependency.write_text(
            "# T-099\n\nState: Done\nDepends-On: none\n",
            encoding="utf-8",
        )
        ticket = self.product / "factory/tickets/T-103.md"
        ticket.write_text(ticket.read_text().replace(
            "Depends-On: none", "Depends-On: T-099",
        ))
        run(self.product, "git", "add", "factory/tickets")
        run(self.product, "git", "commit", "-qm", "unattested dependency")
        run(
            self.product, "git", "update-ref", "refs/remotes/origin/main",
            run(self.product, "git", "rev-parse", "HEAD"),
        )
        with (
            mock.patch.object(
                ENVIRONMENT, "initialize_selected_operator",
                side_effect=AssertionError("operator initialization must not run"),
            ),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError,
                "T-103: qualification dependency lacks protected evidence: T-099",
            ),
            mock.patch.object(
                ENVIRONMENT, "protected_dependency",
                side_effect=ENVIRONMENT.TerminalError(
                    "protected main lacks dependency fulfillment evidence"
                ),
            ),
        ):
            ENVIRONMENT.prepare(argparse.Namespace(
                factory_root=self.factory, product_root=self.product,
                project="relay", root=self.root,
            ))
        self.assertFalse((self.root / "marker.json").exists())

    def test_accepts_external_dependency_fulfillment(self) -> None:
        ticket = self.product / "factory/tickets/T-103.md"
        ticket.write_text(ticket.read_text().replace(
            "Depends-On: none", "Depends-On: T-099",
        ))
        run(self.product, "git", "add", "factory/tickets/T-103.md")
        run(self.product, "git", "commit", "-qm", "fulfilled dependency")
        run(
            self.product, "git", "update-ref", "refs/remotes/origin/main",
            run(self.product, "git", "rev-parse", "HEAD"),
        )
        with mock.patch.object(
            ENVIRONMENT, "protected_dependency", return_value={
                "basis": "validated-protected-dependency-fulfillment",
                "ticket": "T-099",
            },
        ) as dependency:
            ENVIRONMENT.validate_selected_contracts(self.product)
        dependency.assert_called_once_with(self.product, "T-099")

    def test_state_changing_clis_reject_malformed_tickets_before_state(self) -> None:
        home = (self.root / "raw-cli-home").resolve()
        home.mkdir(mode=0o700)
        kits = home / ".factory/kits"
        kits.mkdir(parents=True)
        trace = self.workspace / "raw-cli.trace"
        binary = home / ".factory/bin"
        binary.mkdir(parents=True)
        for name in ("claude", "codex", "cursor", "gh"):
            path = binary / name
            path.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$0\" >> \"$FACTORY_CLI_TRACE\"\n",
                encoding="utf-8",
            )
            path.chmod(0o700)
        common = {
            **os.environ,
            "FACTORY_CLI_TRACE": str(trace),
            "FACTORY_KITS_ROOT": str(kits),
            "HOME": str(home),
        }
        launcher_environment = {
            **common,
            "FACTORY_LAUNCH_TEST_HOME": str(home),
            "FACTORY_LAUNCH_TEST_MODE": "1",
        }
        kit_environment = {
            **common,
            "FACTORY_KIT_CANONICAL_ORIGIN": str(self.factory),
            "FACTORY_KIT_TEST_MODE": "1",
            "FACTORY_RELEASE_TEST_HOME": str(home),
        }
        malformed = (
            "../T-1", "T-1 ", " T-1", "T‐1", "T-١", "T-1\n",
        )
        for ticket in malformed:
            launcher = subprocess.run(
                [
                    "/bin/bash", str(ROOT / "scripts/factory-launch"),
                    "qualification-test", "claim", "--ticket", ticket,
                    "--workdir", str(self.product), "--json",
                ],
                text=True, capture_output=True, env=launcher_environment,
            )
            self.assertEqual(launcher.returncode, 2, launcher.stdout)
            self.assertEqual(
                launcher.stderr, "factory-launch: invalid ticket identifier\n",
            )
            kit = subprocess.run(
                [
                    "/bin/bash", str(ROOT / "scripts/factory-kit.sh"),
                    "operator", "ready", "--project", "qualification-test",
                    "--product", str(self.product), "--ticket", ticket,
                ],
                text=True, capture_output=True, env=kit_environment,
            )
            self.assertEqual(kit.returncode, 1, kit.stderr)
            self.assertIn("invalid ticket identifier", kit.stderr)
            self.assertEqual(list(kits.iterdir()), [])
            self.assertFalse(trace.exists())

    def test_doctor_classifies_authenticated_artifact_tamper_read_only(self) -> None:
        state = (self.workspace / "doctor-controller").resolve()
        events = state / "events"
        passports = state / "passports"
        events.mkdir(parents=True, mode=0o700)
        passports.mkdir(mode=0o700)
        state.chmod(0o700)
        canonical = lambda value: (
            json.dumps(
                value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
            ) + "\n"
        ).encode()
        receipt = {
            "branch": "ticket/T-101", "consumed": False,
            "contract_version": "2.0.0", "factory_sha": self.sha,
            "head_sha": "a" * 40, "project": "relay", "role": "planner",
            "schema": "nysa.software-factory.transition-receipt/v1",
            "stage": "RUN planner", "ticket": "T-101",
        }
        immutable = {
            name: value for name, value in receipt.items()
            if name not in {"consumed", "consumed_at_epoch", "receipt_sha256"}
        }
        receipt["receipt_sha256"] = hashlib.sha256(canonical(immutable)).hexdigest()
        receipt_path = state / "T-101.json"
        receipt_path.write_bytes(canonical(receipt))
        receipt_path.chmod(0o600)
        secret = b"k" * 32
        key = state / "passport.key"
        key.write_bytes(secret)
        key.chmod(0o600)
        passport = {
            "branch": "ticket/T-101", "contract_version": "2.0.0",
            "current_state": "Planning", "factory_sha": self.sha,
            "project": "relay", "publication_state": "none",
            "schema": "nysa.software-factory.ticket-passport/v1",
            "ticket": "T-101",
            "transition_receipt_sha256": receipt["receipt_sha256"],
        }
        passport["authentication_sha256"] = hmac.new(
            secret, canonical(passport), hashlib.sha256,
        ).hexdigest()
        passport["passport_sha256"] = hashlib.sha256(canonical(passport)).hexdigest()
        passport_path = passports / "T-101.json"
        passport_path.write_bytes(canonical(passport))
        passport_path.chmod(0o600)
        event = {
            "event": "ticket_released", "factory_sha": None,
            "observed_at_epoch_ns": 1,
            "schema": "nysa.software-factory.controller-event/v1",
            "ticket": "T-101",
        }
        event["event_sha256"] = hashlib.sha256(json.dumps(
            event, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        event_path = events / "1.json"
        event_path.write_text(json.dumps(event), encoding="utf-8")
        event_path.chmod(0o600)
        originals = {
            "receipt": receipt_path.read_bytes(),
            "passport": passport_path.read_bytes(),
            "event": event_path.read_bytes(),
        }
        trace = self.workspace / "doctor-provider.trace"
        binary = self.workspace / "doctor-bin"
        binary.mkdir()
        for name in ("claude", "codex", "agent", "gh"):
            path = binary / name
            path.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$0\" >> \"$FACTORY_CLI_TRACE\"\n",
                encoding="utf-8",
            )
            path.chmod(0o700)
        environment = {
            **os.environ,
            "FACTORY_ADAPTER_OVERRIDE": "mock",
            "FACTORY_CLI_TRACE": str(trace),
            "FACTORY_CONTROLLER_STATE_DIR": str(state),
            "FACTORY_KIT_TRUST_SCOPE": "repository-test",
            "FACTORY_TEST_MODE": "1",
            "FACTORY_TRUSTED_TEST_HARNESS": "1",
            "PATH": f"{binary}:/usr/bin:/bin",
        }
        before = sorted(
            (path.relative_to(state).as_posix(), path.read_bytes())
            for path in state.rglob("*") if path.is_file()
        )
        product_sha = run(self.product, "git", "rev-parse", "HEAD")
        product_tree = run(self.product, "git", "rev-parse", "HEAD^{tree}")
        factory_tree = run(self.factory, "git", "rev-parse", "HEAD^{tree}")
        certification_runtime = json.loads(
            (self.product / "factory/certification-plan.json").read_text()
        )["runtime"]
        runtime_tuple = json.dumps({
            "contract_version": "2.0.0",
            "factory_sha": self.sha,
            "factory_tree": factory_tree,
            "node": certification_runtime["node"],
            "npm": certification_runtime["npm"],
            "product_sha": product_sha,
            "product_tree": product_tree,
        }, sort_keys=True)
        candidate_environment = {
            **environment,
            "FACTORY_CERTIFICATION_TUPLE": runtime_tuple,
            "FACTORY_KIT_TRUST_SCOPE": "qualification-candidate",
            "FACTORY_QUALIFICATION_PRODUCT_SHA": product_sha,
            "FACTORY_QUALIFICATION_PRODUCT_TREE": product_tree,
            "FACTORY_RELEASE_TREE": factory_tree,
            "PATH": (
                f"{binary}:{self.home / '.local/bin'}:/usr/bin:/bin"
            ),
        }

        def doctor(env: dict[str, str]) -> dict[str, object]:
            def snapshot(root: Path) -> list[tuple[str, int, object]]:
                result = []
                for path in root.rglob("*"):
                    value = path.lstat()
                    content = (
                        os.readlink(path) if path.is_symlink()
                        else path.read_bytes() if path.is_file()
                        else None
                    )
                    result.append((path.relative_to(root).as_posix(), value.st_mode, content))
                return sorted(result)

            persisted = snapshot(state), snapshot(self.product / "factory")
            result = subprocess.run(
                [
                    "/bin/bash", str(ROOT / "scripts/factory-doctor.sh"), "--json",
                    "--project", "relay", "--kit-dir", str(self.factory),
                    "--product-root", str(self.product), "--kit-sha", self.sha,
                ],
                text=True, capture_output=True, env=env,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(
                (snapshot(state), snapshot(self.product / "factory")), persisted,
            )
            return json.loads(result.stdout)["checks"]

        plan = self.product / "factory/certification-plan.json"
        plan_raw = plan.read_bytes()
        pin = self.product / "factory/KIT_PIN"
        pin_raw = pin.read_bytes()
        active_runs = self.product / "factory/.active-runs"
        ticket_paths = [
            self.product / f"factory/tickets/T-{number}.md"
            for number in (101, 102, 103)
        ]
        ticket_raw = {path: path.read_bytes() for path in ticket_paths}
        ticket_environment = {
            **candidate_environment,
            "FACTORY_QUALIFICATION_MANIFEST": str(
                self.product / "factory/QUALIFICATION.json"
            ),
        }

        value = json.loads(originals["receipt"])
        value["receipt_sha256"] = "0" * 64
        receipt_path.write_text(json.dumps(value), encoding="utf-8")
        receipt_path.chmod(0o600)
        plan.write_bytes(plan_raw + b" ")
        pin.write_text("0" * 40 + "\n", encoding="utf-8")
        active_runs.mkdir(mode=0o700)
        (active_runs / "malformed").write_text("residue\n", encoding="utf-8")
        ticket_paths[0].write_bytes(ticket_raw[ticket_paths[0]] + b"State: Building\n")
        ticket_paths[1].write_bytes(ticket_raw[ticket_paths[1]].replace(
            b"Depends-On: none", b"Depends-On: ../T-9",
        ))
        ticket_paths[2].unlink()
        ticket_paths[2].symlink_to("T-102.md")
        checks = doctor(ticket_environment)
        self.assertEqual(checks["authenticated_artifacts"], {
            "reason_code": "transition_receipt_invalid", "status": "error",
        })
        self.assertEqual(checks["qualification_identity"], {
            "reason_code": "product_identity_drift", "status": "error",
        })
        self.assertEqual(checks["kit_pin"], {
            "full_sha": "0" * 40,
            "matches_kit": False,
            "path": str(pin),
            "reason_code": "kit_pin_identity_drift",
            "status": "error",
            "valid_full_sha": True,
        })
        self.assertEqual(
            checks["runtime"]["reason_code"], "runtime_residue_invalid",
        )
        readiness = checks["qualification_ticket_readiness"]
        self.assertEqual(readiness["status"], "error")
        self.assertEqual({
            item["ticket"]: item["reason_code"]
            for item in readiness["tickets"] if item["status"] == "error"
        }, {
            "T-101": "ticket_state_conflict",
            "T-102": "ticket_dependencies_invalid",
            "T-103": "ticket_file_unsafe",
        })
        receipt_path.write_bytes(originals["receipt"])
        receipt_path.chmod(0o600)
        plan.write_bytes(plan_raw)
        pin.write_bytes(pin_raw)
        (active_runs / "malformed").unlink()
        active_runs.rmdir()
        for path in ticket_paths:
            path.unlink()
            path.write_bytes(ticket_raw[path])

        wrong_tuple = json.loads(runtime_tuple)
        wrong_tuple["node"] = "v99.0.0"
        drift_environment = {
            **candidate_environment,
            "FACTORY_CERTIFICATION_TUPLE": json.dumps(wrong_tuple),
        }
        value = json.loads(originals["passport"])
        value["authentication_sha256"] = "0" * 64
        passport_path.write_text(json.dumps(value), encoding="utf-8")
        passport_path.chmod(0o600)
        checks = doctor(drift_environment)
        self.assertEqual(checks["authenticated_artifacts"], {
            "reason_code": "ticket_passport_invalid", "status": "error",
        })
        self.assertEqual(checks["qualification_identity"], {
            "reason_code": "certification_plan_identity_drift",
            "status": "error",
        })
        passport_path.write_bytes(originals["passport"])
        passport_path.chmod(0o600)

        value = json.loads(originals["event"])
        value["event_sha256"] = "0" * 64
        event_path.write_text(json.dumps(value), encoding="utf-8")
        event_path.chmod(0o600)
        self.assertEqual(doctor(candidate_environment)["authenticated_artifacts"], {
            "reason_code": "controller_event_invalid", "status": "error",
        })
        event_path.write_bytes(originals["event"])
        event_path.chmod(0o600)
        self.assertFalse(trace.exists())
        self.assertEqual(
            sorted(
                (path.relative_to(state).as_posix(), path.read_bytes())
                for path in state.rglob("*") if path.is_file()
            ),
            before,
        )

    def test_selected_ticket_authoring_fields_fail_before_lane_creation(self) -> None:
        ticket = self.product / "factory/tickets/T-101.md"
        original = ticket.read_text()

        def set_field(name: str, *field_values: str) -> None:
            text = re.sub(
                rf"^{re.escape(name)}:[^\r\n]*\n?", "", original,
                flags=re.IGNORECASE | re.MULTILINE,
            ).rstrip()
            suffix = "".join(f"\n{name}: {value}" for value in field_values)
            ticket.write_text(text + suffix + "\n", encoding="utf-8")

        def rejects(name: str, *field_values: str) -> None:
            set_field(name, *field_values)
            with self.assertRaises(ENVIRONMENT.EnvironmentError) as failure:
                ENVIRONMENT.validate_selected_contracts(self.product)
            self.assertIn(name, str(failure.exception))

        required = {
            "State": "Ready",
            "Priority": "normal",
            "Initiative": "I-001",
            "Depends-On": "none",
            "Product-Decisions": "frozen",
            "Builder ownership": "app/server.js only",
            "Fixture-Seams": "none",
            "Authentication-Seams": "none",
            "Protected-Test-Conflicts": "none",
        }
        for name, value in required.items():
            with self.subTest(field=name, case="missing"):
                rejects(name)
            with self.subTest(field=name, case="empty"):
                rejects(name, "")
            with self.subTest(field=name, case="duplicate"):
                rejects(name, value, value)

        invalid_values = {
            "State": ("Queued", "ready — inherited"),
            "Priority": ("medium", "critical", "none — canceled"),
            "Initiative": (
                "none", "i-001", "I-ABC", "I-001 — inherited", "I-999",
            ),
            "Depends-On": (
                "T-101", "T-099, T-099", "T-099,", "none, T-099",
                "none — rationale", "t-099",
            ),
            "Product-Decisions": ("open", "frozen — inherited"),
            "Builder ownership": (
                "app/ only", "/app/server.js only", "app/server.js",
                "app/server.js, app/server.js only",
            ),
            "Fixture-Seams": (
                "missing.test.ts", "/tests/fixture.js", "../fixture.js",
            ),
            "Authentication-Seams": (
                "/app/server.js", "../app/server.js",
            ),
            "Protected-Test-Conflicts": ("unknown", "../test.js => guard"),
        }
        for name, values in invalid_values.items():
            for value in values:
                with self.subTest(field=name, value=value):
                    rejects(name, value)

        initiatives = self.product / "factory/initiatives"
        initiatives.joinpath("I-002.md").write_text("# untracked\n", encoding="utf-8")
        rejects("Initiative", "I-002")
        initiatives.joinpath("I-003.md").mkdir()
        rejects("Initiative", "I-003")
        initiatives.joinpath("I-004.md").symlink_to("I-001.md")
        rejects("Initiative", "I-004")

        for policy in ("manual", "auto"):
            set_field("Merge-Policy", policy)
            ENVIRONMENT.validate_selected_contracts(self.product)
        for values in (("",), ("automatic",), ("manual", "auto")):
            with self.subTest(field="Merge-Policy", values=values):
                rejects("Merge-Policy", *values)

        for values in (
            ("",), ("Receipt",), ("Linear",), ("Migration",),
            ("unknown",), ("Receipt", "Receipt"),
        ):
            with self.subTest(field="Operator-Approval", values=values):
                rejects("Operator-Approval", *values)
        ticket.write_text(
            original.replace("State: Ready", "State: Approved"), encoding="utf-8",
        )
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "Operator-Approval",
        ):
            ENVIRONMENT.validate_selected_contracts(self.product)
        for approval in ("Linear", "Receipt"):
            ticket.write_text(
                original.replace("State: Ready", "State: Approved")
                + f"Operator-Approval: {approval}\n",
                encoding="utf-8",
            )
            ENVIRONMENT.validate_selected_contracts(self.product)
        for values in (("",), ("Approved",), ("Planning", "Review")):
            with self.subTest(field="Resume-State", values=values):
                rejects("Resume-State", *values)
        for state in ("Backlog", "Ready", "Planning", "Building", "Review"):
            set_field("Resume-State", state)
            ENVIRONMENT.validate_selected_contracts(self.product)

        for priority in ("none", "urgent", "high", "normal", "low"):
            set_field("Priority", priority)
            ENVIRONMENT.validate_selected_contracts(self.product)

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

        ticket.write_text(original, encoding="utf-8")
        ticket.unlink()
        ticket.symlink_to("T-102.md")
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "ticket contract is unsafe",
        ):
            ENVIRONMENT.validate_selected_contracts(self.product)
        ticket.unlink()
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

    def test_rejects_protected_ready_builder_ownership_conflicts(self) -> None:
        candidate = self.product / "factory/tickets/T-605.md"
        candidate.write_text(
            "# T-605\n\nState: Ready\n"
            "Builder ownership: app/server.js only\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError,
            r"qualification Builder ownership conflict app/server.js: T-101,T-605",
        ):
            ENVIRONMENT.validate_selected_contracts(self.product)

        candidate.write_text(candidate.read_text().replace("State: Ready", "State: Backlog"))
        ENVIRONMENT.validate_selected_contracts(self.product)

        candidate.unlink()
        ticket = self.product / "factory/tickets/T-102.md"
        ticket.write_text(ticket.read_text().replace("app/worker.js", "app/server.js"))
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError,
            r"qualification Builder ownership conflict app/server.js: T-101,T-102",
        ):
            ENVIRONMENT.validate_selected_contracts(self.product)

    def test_rejects_selected_protected_source_hash_before_lane_creation(self) -> None:
        (self.product / "app").mkdir()
        (self.product / "tests").mkdir()
        (self.product / "app/server.js").write_text("export const value = 1;\n")
        (self.product / "tests/source-boundary.test.js").write_text(
            "import { createHash } from 'node:crypto';\n"
            "import { readFileSync } from 'node:fs';\n"
            "const frozen = [['../app/server.js', "
            "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa']];\n"
            "const digest = createHash('sha256').update(readFileSync(\n"
            "  new URL(frozen[0][0], import.meta.url),\n"
            ")).digest('hex');\n"
        )
        run(self.product, "git", "add", "app", "tests")
        run(self.product, "git", "commit", "-qm", "protected source boundary")
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError,
            r"READINESS BLOCKED: protected source hash collision: "
            r"tests/source-boundary.test.js => app/server.js",
        ):
            ENVIRONMENT.validate_selected_contracts(self.product)

        (self.product / "tests/source-boundary.test.js").write_text(
            "import { readFileSync } from 'node:fs';\n"
            "const source = readFileSync('app/server.js', 'utf8');\n"
            "expect(source).toContain('export const value');\n"
        )
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError,
            r"READINESS BLOCKED: protected source assertion collision: "
            r"tests/source-boundary.test.js => app/server.js",
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
                    "qualification manifest is invalid",
                ):
                    ENVIRONMENT.qualification_manifest(self.product, self.sha)
        path.write_text(json.dumps(original) + "\n")

    def test_selected_operator_refreshes_already_initialized_cohort(self) -> None:
        mapping = self.workspace / "selected-operator-map.json"
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
        state_dir = self.workspace / "selected-controller"
        completed = subprocess.CompletedProcess([], 0, "", "")
        with (
            mock.patch.object(
                ENVIRONMENT, "committed_ticket",
                return_value=("State: Ready\n", "HEAD"),
            ),
            mock.patch.object(
                ENVIRONMENT.subprocess, "run", return_value=completed,
            ) as invoked,
        ):
            ENVIRONMENT.initialize_selected_operator(
                self.factory, self.product, mapping, ledger, state_dir,
                refresh=True,
            )
        self.assertEqual(invoked.call_count, 4)
        self.assertEqual(
            [call.args[0][-2:] for call in invoked.call_args_list[:3]],
            [["--ticket", "T-101"], ["--ticket", "T-102"],
             ["--ticket", "T-103"]],
        )
        self.assertEqual(
            [call.args[0][-3] for call in invoked.call_args_list[:3]],
            ["init", "init", "init"],
        )
        self.assertEqual(
            invoked.call_args_list[3].args[0][-2:],
            ["--runtime-ledger", str(ledger)],
        )
        for call in invoked.call_args_list:
            self.assertEqual(call.kwargs["env"]["FACTORY_OPERATOR_MAP"], str(mapping))
            self.assertEqual(
                call.kwargs["env"]["FACTORY_CONTROLLER_STATE_DIR"], str(state_dir),
            )

    def test_selected_operator_retries_pending_ready_projection(self) -> None:
        mapping = self.workspace / "pending-ready-operator-map.json"
        ENVIRONMENT.write(mapping, {
            "_config": {},
            "_sync": {"selected_ticket_success_at": {
                "T-101": "2026-08-18T00:00:00Z",
            }},
            "initiatives": {},
            "tickets": {
                "T-101": {
                    "operator_fields_initialized": True,
                    "operator": {
                        "observed_at": "2026-08-18T00:00:00Z",
                        "receipt_sha256": "a" * 64,
                        "state": "Ready",
                        "state_base": "backlog",
                    },
                },
            },
        })
        ledger = self.workspace / "pending-ready-runtime-ledger.csv"
        state_dir = self.workspace / "pending-ready-controller"
        commands = []

        def complete(command, **_kwargs):
            commands.append(command)
            if "operator-cli.py" in command[1]:
                ticket = command[-1]
                value = ENVIRONMENT.read(mapping)
                entry = value["tickets"].setdefault(ticket, {})
                entry["operator_fields_initialized"] = True
                entry.pop("operator", None)
                value["_sync"].setdefault(
                    "selected_ticket_success_at", {}
                )[ticket] = "2026-08-18T00:01:00Z"
                ENVIRONMENT.replace(mapping, value)
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            mock.patch.object(
                ENVIRONMENT, "committed_ticket",
                return_value=("State: Backlog\n", "HEAD"),
            ),
            mock.patch.object(ENVIRONMENT.subprocess, "run", side_effect=complete),
        ):
            ENVIRONMENT.initialize_selected_operator(
                self.factory, self.product, mapping, ledger, state_dir,
            )

        self.assertEqual(
            [(command[-3], command[-1]) for command in commands[:3]],
            [("ready", "T-101"), ("ready", "T-102"), ("ready", "T-103")],
        )

    def test_selected_operator_readies_backlog_cohort(self) -> None:
        mapping = self.workspace / "backlog-operator-map.json"
        ENVIRONMENT.write(mapping, {
            "_config": {}, "_sync": {}, "initiatives": {}, "tickets": {},
        })
        ledger = self.workspace / "backlog-runtime-ledger.csv"
        state_dir = self.workspace / "backlog-controller"
        commands = []

        def complete(command, **_kwargs):
            commands.append(command)
            if "operator-cli.py" in command[1]:
                ticket = command[-1]
                value = ENVIRONMENT.read(mapping)
                value["tickets"][ticket] = {
                    "operator_fields_initialized": True,
                }
                value["_sync"].setdefault(
                    "selected_ticket_success_at", {}
                )[ticket] = "2026-08-14T00:00:00Z"
                ENVIRONMENT.replace(mapping, value)
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            mock.patch.object(
                ENVIRONMENT, "committed_ticket",
                return_value=("State: Backlog\n", "HEAD"),
            ),
            mock.patch.object(ENVIRONMENT.subprocess, "run", side_effect=complete),
        ):
            ENVIRONMENT.initialize_selected_operator(
                self.factory, self.product, mapping, ledger, state_dir,
            )

        self.assertEqual(
            [command[-3] for command in commands[:3]],
            ["ready", "ready", "ready"],
        )

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
        (publisher / "factory/tickets/T-030.md").parent.mkdir()
        (publisher / "factory/tickets/T-030.md").write_text(
            "# T-030\n\nState: Done\n", encoding="utf-8",
        )
        (publisher / "factory/tickets/T-031.md").write_text(
            "# T-031\n\nState: Approved\n", encoding="utf-8",
        )
        run(publisher, "git", "add", ".")
        run(publisher, "git", "commit", "-qm", "base")
        base = run(publisher, "git", "rev-parse", "HEAD")
        run(publisher, "git", "remote", "add", "origin", str(remote))
        run(publisher, "git", "push", "-q", "origin", "main")
        run(publisher, "git", "switch", "-qc", "reviewed")
        (publisher / "reviewed.txt").write_text("reviewed\n", encoding="utf-8")
        run(publisher, "git", "add", ".")
        run(publisher, "git", "commit", "-qm", "reviewed")
        reviewed = run(publisher, "git", "rev-parse", "HEAD")
        run(publisher, "git", "push", "-q", "origin", "reviewed")
        run(publisher, "git", "switch", "-q", "main")
        run(publisher, "git", "switch", "-qc", "route-blob")
        (publisher / "route.json").write_text("{}\n", encoding="utf-8")
        run(publisher, "git", "add", ".")
        run(publisher, "git", "commit", "-qm", "route blob")
        route_blob = run(publisher, "git", "rev-parse", "HEAD:route.json")
        run(publisher, "git", "push", "-q", "origin", "route-blob")
        run(publisher, "git", "switch", "-q", "main")
        run(publisher, "git", "switch", "-qc", "ticket/T-030")
        (publisher / "evidence.txt").write_text("evidence\n", encoding="utf-8")
        attestation = publisher / "factory/attestations/T-030/bundle.json"
        attestation.parent.mkdir(parents=True)
        attestation_text = json.dumps({
            "branch_head": base, "reviewed_sha": reviewed,
            "route_plan_blob": route_blob,
        }) + "\n"
        attestation.write_text(attestation_text, encoding="utf-8")
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
        main_attestation = publisher / "factory/attestations/T-030/bundle.json"
        main_attestation.parent.mkdir(parents=True)
        main_attestation.write_text(attestation_text, encoding="utf-8")
        skipped_object = "f" * 40
        skipped = publisher / "factory/attestations/T-031/bundle.json"
        skipped.parent.mkdir(parents=True)
        skipped.write_text(json.dumps({
            "branch_head": skipped_object, "reviewed_sha": skipped_object,
        }) + "\n", encoding="utf-8")
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
        self.assertFalse(ENVIRONMENT.commit_present(consumer, reviewed))
        with self.assertRaises(subprocess.CalledProcessError):
            run(consumer, "git", "cat-file", "-e", route_blob)
        refs = run(consumer, "git", "show-ref")
        fetch_head = consumer / ".git/FETCH_HEAD"
        fetch_head_before = fetch_head.read_bytes() if fetch_head.exists() else None
        redirected = self.workspace / "redirected.git"
        run(self.workspace, "git", "init", "--bare", "-q", str(redirected))
        run(
            consumer, "git", "config",
            f"url.{redirected}.insteadOf", str(remote),
        )
        ambient_objects = self.workspace / "ambient-objects"
        ambient_objects.mkdir()
        with mock.patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": str(consumer / ".git/config"),
            "GIT_OBJECT_DIRECTORY": str(ambient_objects),
            "GIT_SSH_COMMAND": "false",
        }):
            self.assertEqual(
                ENVIRONMENT.historical_pr_objects(consumer, str(remote)), 1,
            )
        self.assertEqual(list(ambient_objects.iterdir()), [])
        self.assertTrue(ENVIRONMENT.commit_present(consumer, head))
        self.assertTrue(ENVIRONMENT.commit_present(consumer, evidence))
        self.assertTrue(ENVIRONMENT.commit_present(consumer, reviewed))
        self.assertEqual(run(consumer, "git", "cat-file", "-t", route_blob), "blob")
        with self.assertRaises(subprocess.CalledProcessError):
            run(consumer, "git", "cat-file", "-e", skipped_object)
        self.assertEqual(run(consumer, "git", "show-ref"), refs)
        self.assertEqual(fetch_head.read_bytes() if fetch_head.exists() else None, fetch_head_before)
        run(consumer, "git", "remote", "set-url", "origin", "invalid://offline")
        self.assertEqual(ENVIRONMENT.historical_pr_objects(consumer, str(remote)), 1)

        attestation_consumer = self.workspace / "attestation-consumer"
        run(
            self.workspace, "git", "clone", "-q", "--no-local",
            "--single-branch", "--branch", "main", str(remote),
            str(attestation_consumer),
        )
        shutil.rmtree(attestation_consumer / "factory/migrations")
        self.assertFalse(ENVIRONMENT.commit_present(attestation_consumer, reviewed))
        with self.assertRaises(subprocess.CalledProcessError):
            run(attestation_consumer, "git", "cat-file", "-e", route_blob)
        refs = run(attestation_consumer, "git", "show-ref")
        fetch_head = attestation_consumer / ".git/FETCH_HEAD"
        fetch_head_before = fetch_head.read_bytes() if fetch_head.exists() else None
        self.assertEqual(
            ENVIRONMENT.historical_pr_objects(attestation_consumer, str(remote)), 0,
        )
        self.assertTrue(ENVIRONMENT.commit_present(attestation_consumer, reviewed))
        self.assertEqual(
            run(attestation_consumer, "git", "cat-file", "-t", route_blob), "blob",
        )
        self.assertEqual(run(attestation_consumer, "git", "show-ref"), refs)
        self.assertEqual(
            fetch_head.read_bytes() if fetch_head.exists() else None,
            fetch_head_before,
        )

    def test_historical_pr_ref_mismatch_fails_closed(self) -> None:
        remote = self.workspace / "mismatch.git"
        run(self.workspace, "git", "init", "--bare", "-q", str(remote))
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
            ENVIRONMENT.historical_pr_objects(self.product, str(remote))
        self.assertFalse((self.root / "marker.json").exists())

    def test_takeover_reuses_authenticated_live_state_without_copying_it(self) -> None:
        source_sha = "b" * 40
        intermediate_sha = "d" * 40
        tickets = ["T-094", "T-100", "T-093"]
        (self.product / "factory/KIT_PIN").write_text(
            source_sha + "\n", encoding="utf-8",
        )
        (self.product / ".gitignore").write_text(
            "factory/operator-map.json\n", encoding="utf-8",
        )
        for ticket in tickets:
            (self.product / f"factory/tickets/{ticket}.md").write_text(
                f"# {ticket}\n\nState: Ready\nProduct-Decisions: frozen\n"
                "Initiative: I-001\nPriority: normal\nDepends-On: none\nFixture-Seams: none\n"
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
        operator_map = source_product / "factory/operator-map.json"
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
        account_runtime = account / ".local/bin"
        account_runtime.mkdir(parents=True, mode=0o700)
        for tool in ("node", "npm", "npx"):
            account_runtime.joinpath(tool).symlink_to(
                self.home / ".local/bin" / tool,
            )
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
        self.assertFalse((self.product / "factory/operator-map.json").exists())
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
        (self.factory / "factory-contract.json").write_text(
            '{"contract_version":"1.9.0"}\n', encoding="utf-8",
        )
        run(self.factory, "git", "add", "factory-contract.json")
        run(self.factory, "git", "commit", "-qm", "start Contract 1.9 lane")
        source = run(self.factory, "git", "rev-parse", "HEAD")
        manifest_path = self.product / "factory/QUALIFICATION.json"
        manifest = json.loads(manifest_path.read_text())
        manifest.update(contract_version="1.9.0", factory_sha=source)
        manifest_path.write_text(json.dumps(manifest) + "\n")
        (self.product / "factory/KIT_PIN").write_text(
            source + "\n", encoding="utf-8",
        )
        run(
            self.product, "git", "add", "factory/KIT_PIN",
            "factory/QUALIFICATION.json",
        )
        run(self.product, "git", "commit", "-qm", "qualify Contract 1.9")
        first = ENVIRONMENT.prepare(args)
        active_path = self.root / "projects/relay/active.json"
        legacy_active = ENVIRONMENT.read(active_path)
        legacy_receipt_path = (
            self.root / "receipts" / f"{legacy_active['receipt_id']}.json"
        )
        legacy_receipt = legacy_receipt_path.read_bytes()
        self.assertEqual(
            ENVIRONMENT.qualification_lane(self.root, "relay")["active"][
                "contract_version"
            ],
            "1.9.0",
        )
        legacy_active.pop("product_sha")
        legacy_active.pop("runtime_tuple")
        ENVIRONMENT.replace(active_path, legacy_active)
        controller = Path(first["authority_root"]) / "controller"
        authority_path = Path(first["authority_root"]) / "authority.json"
        legacy_authority = ENVIRONMENT.read(authority_path)
        legacy_authority.pop("authority_sha256")
        legacy_authority["contract_version"] = "1.8.0"
        legacy_authority["authority_sha256"] = hashlib.sha256(
            ENVIRONMENT.canonical(legacy_authority)
        ).hexdigest()
        ENVIRONMENT.replace(authority_path, legacy_authority)
        claims = controller / "claims"
        self.assertTrue(controller.is_dir())
        self.assertEqual(controller.stat().st_mode & 0o777, 0o700)
        claims.mkdir(mode=0o700)
        key = controller / "passport.key"
        key.write_bytes(b"p" * 32)
        key.chmod(0o600)
        ENVIRONMENT.write(claims / "T-110.json", {"status": "running"})

        (self.factory / "factory-contract.json").write_text(
            '{"contract_version":"2.0.0"}\n', encoding="utf-8",
        )
        run(self.factory, "git", "add", "factory-contract.json")
        run(self.factory, "git", "commit", "-qm", "successor Contract 2.0")
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
            "authority": authority_path.read_bytes(),
        }
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError,
            "qualification manifest is invalid",
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
            authority_path.read_bytes(),
            before["authority"],
        )

        manifest = json.loads(manifest_path.read_text())
        manifest.update(contract_version="2.0.0", factory_sha=successor)
        manifest_path.write_text(json.dumps(manifest) + "\n")
        run(self.product, "git", "add", "factory/QUALIFICATION.json")
        run(self.product, "git", "commit", "-qm", "authorize successor")

        before_pin_refusal = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*") if path.is_file()
        }
        with (
            mock.patch.dict(
                os.environ, {"FACTORY_TEST_PROVIDER_PIN_UNREADY": "1"},
            ),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError,
                "qualification candidate provider CLI pins are not ready",
            ),
        ):
            ENVIRONMENT.upgrade(argparse.Namespace(
                **vars(args), global_env=replacement,
            ))
        self.assertEqual(before_pin_refusal, {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*") if path.is_file()
        })
        self.assertFalse((self.root / f"releases/{successor}").exists())

        original_replace = ENVIRONMENT.replace
        crashed = False

        def crash_before_active(path, value):
            nonlocal crashed
            original_replace(path, value)
            if not crashed and path.name == "environment.json":
                crashed = True
                raise ENVIRONMENT.EnvironmentError("simulated response loss")

        with (
            mock.patch.object(
                ENVIRONMENT, "replace", side_effect=crash_before_active,
            ),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "simulated response loss",
            ),
        ):
            ENVIRONMENT.upgrade(argparse.Namespace(
                **vars(args), global_env=replacement,
            ))
        self.assertEqual(
            ENVIRONMENT.read(active_path)["contract_version"], "1.9.0",
        )
        self.assertEqual(
            ENVIRONMENT.read(self.root / "environment.json")["status"], "upgraded",
        )
        response_lost = False

        def lose_response_after_active(path, value):
            nonlocal response_lost
            original_replace(path, value)
            if not response_lost and path == active_path:
                response_lost = True
                raise ENVIRONMENT.EnvironmentError("simulated response loss after active switch")

        with (
            mock.patch.object(
                ENVIRONMENT, "replace", side_effect=lose_response_after_active,
            ),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "response loss after active switch",
            ),
        ):
            ENVIRONMENT.upgrade(argparse.Namespace(
                **vars(args), global_env=replacement,
            ))
        active_before_replay = active_path.read_bytes()
        receipts_before_replay = sorted(
            path.name for path in (self.root / "receipts").iterdir()
        )
        second = ENVIRONMENT.upgrade(argparse.Namespace(
            **vars(args), global_env=replacement,
        ))
        self.assertEqual(active_path.read_bytes(), active_before_replay)
        self.assertEqual(
            sorted(path.name for path in (self.root / "receipts").iterdir()),
            receipts_before_replay,
        )
        active = json.loads(active_path.read_text())
        self.assertEqual(first["status"], "prepared")
        self.assertEqual(second["status"], "upgraded")
        self.assertEqual(active["kit_sha"], successor)
        self.assertEqual(active["contract_version"], "2.0.0")
        self.assertEqual(active["generation"], 2)
        self.assertEqual(active["product_sha"], second["product_sha"])
        self.assertEqual(active["runtime_tuple"], second["runtime_tuple"])
        self.assertEqual(
            (self.root / "global.env").read_bytes(), replacement.read_bytes(),
        )
        self.assertEqual(key.read_bytes(), b"p" * 32)
        self.assertEqual(legacy_receipt_path.read_bytes(), legacy_receipt)
        self.assertEqual(
            ENVIRONMENT.read(legacy_receipt_path)["contract_version"], "1.9.0",
        )
        self.assertEqual(
            ENVIRONMENT.read(
                self.root / "receipts" / f"{active['receipt_id']}.json"
            )["contract_version"],
            "2.0.0",
        )
        self.assertEqual(
            ENVIRONMENT.read(authority_path)["contract_version"],
            "2.0.0",
        )
        self.assertTrue((self.root / f"releases/{source}").is_dir())
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
        for artifact in (self.product / "factory/runs").iterdir():
            artifact.unlink()
        (self.product / "factory/runs").rmdir()
        failed = "a" * 40
        self.write_passport(path, secret, "T-102", source, failed)
        corrected_charge = {
            **charge(failed, "corrected-reviewer"),
            "accounting_state": "abandoned_conservative",
            "role": "reviewer",
        }
        corrected_completion = {
            **completion(failed, "corrected-reviewer"),
            "role": "reviewer",
        }
        correction = {
            "failed_factory_sha": failed,
            "issue": "https://github.com/nysa-company/software-factory/issues/390",
            "output_head_sha": "5" * 40,
            "progress_events": 1,
            "progress_journal_sha256": "6" * 64,
            "receipt_parent_file_sha256": "7" * 64,
            "recovery_factory_sha": source,
            "role": "reviewer",
            "run_id": "corrected-reviewer",
            "schema": "nysa.software-factory.completed-role-correction/v2",
            "transition_receipt_sha256": "3" * 64,
        }
        rewrite(lambda value: value.update(
            charge_records=[corrected_charge],
            completed_role_corrections=[correction],
            completed_role_evidence=[corrected_completion],
            cumulative_charges_micro_usd=1,
        ))
        ENVIRONMENT.validate_successor_upgrade_cohort(
            self.factory, self.product, controller, "relay", source,
            product_sha, manifest,
        )
        rewrite(lambda value: value.update(completed_role_corrections=[]))
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

        intermediate = "d" * 40
        unconsumed = "e" * 40
        for ticket in tickets:
            self.write_passport(
                passports / f"{ticket}.json", secret, ticket, intermediate, source,
            )
        authorization = (
            self.product / "factory/migrations/inflight-release"
            / f"{intermediate}.json"
        )
        authorization.parent.mkdir(parents=True, exist_ok=True)
        authorization.write_text(json.dumps({
            "repository": "example/product",
            "schema": "nysa.software-factory.inflight-release-authorization/v1",
            "source_kit_sha": source,
            "target_kit_sha": intermediate,
            "tickets": [{
                "branch": f"ticket/{ticket}",
                "head": f"{index}" * 40,
                "state": "Building",
                "ticket": ticket,
            } for index, ticket in enumerate(tickets, 1)],
        }, sort_keys=True, separators=(",", ":")) + "\n")
        run(self.product, "git", "add", str(authorization))
        run(self.product, "git", "commit", "-qm", "authorize prior candidate")
        authorized_product = run(self.product, "git", "rev-parse", "HEAD")
        ENVIRONMENT.validate_successor_upgrade_cohort(
            self.factory, self.product, controller, "relay", unconsumed,
            authorized_product, manifest,
        )
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError,
            "T-101: successor qualification requires every selected ticket",
        ):
            ENVIRONMENT.validate_successor_upgrade_cohort(
                self.factory, self.product, controller, "relay", unconsumed,
                product_sha, manifest,
            )

    def test_successor_upgrade_accepts_only_exact_preserved_checkpoint(self) -> None:
        controller = (self.workspace / "checkpoint-controller").resolve()
        passports = controller / "passports"
        passports.mkdir(mode=0o700, parents=True)
        controller.chmod(0o700)
        secret = b"p" * 32
        key = controller / "passport.key"
        key.write_bytes(secret)
        key.chmod(0o600)
        source = "b" * 40
        candidate = "c" * 40
        ticket = "T-101"
        base = run(self.product, "git", "rev-parse", "HEAD")
        run(self.product, "git", "switch", "-qc", f"ticket/{ticket}")
        ticket_path = self.product / f"factory/tickets/{ticket}.md"
        ticket_path.write_text(
            ticket_path.read_text() + f"Kit-SHA: {source}\n",
            encoding="utf-8",
        )
        route = self.product / f"factory/route-plans/{ticket}.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        run(self.product, "git", "add", "factory")
        run(self.product, "git", "commit", "-qm", "source checkpoint")
        checkpoint_parent = run(self.product, "git", "rev-parse", "HEAD")
        checkpoint_parent_tree = run(
            self.product, "git", "rev-parse", f"{checkpoint_parent}^{{tree}}",
        )
        checkpoint_parent_ticket_blob = run(
            self.product, "git", "rev-parse",
            f"{checkpoint_parent}:factory/tickets/{ticket}.md",
        )
        ticket_path.write_text(
            ticket_path.read_text() + "\npreserved source work\n",
            encoding="utf-8",
        )
        run(self.product, "git", "add", str(ticket_path))
        run(self.product, "git", "commit", "-qm", "preserve source work")
        checkpoint = run(self.product, "git", "rev-parse", "HEAD")
        route_sha256 = hashlib.sha256(route.read_bytes()).hexdigest()
        run(self.product, "git", "switch", "-q", "main")

        passport_path = passports / f"{ticket}.json"

        def write_source_passport(protected: str) -> None:
            self.write_passport(passport_path, secret, ticket, source)
            value = json.loads(passport_path.read_text(encoding="utf-8"))
            value.pop("authentication_sha256")
            value.pop("passport_sha256")
            value.update({
                "base_history": [protected],
                "current_stage": "RUN planner",
                "current_state": "Ready",
                "head_sha": checkpoint_parent,
                "head_tree": checkpoint_parent_tree,
                "migration_history": [{
                    "from_factory_sha": source,
                    "from_head_sha": base,
                    "from_passport_file_sha256": "2" * 64,
                    "from_passport_sha256": "3" * 64,
                    "from_protected_base_sha": protected,
                    "from_route_plan_sha256": route_sha256,
                    "schema": (
                        "nysa.software-factory.ticket-passport-migration/v2"
                    ),
                    "to_factory_sha": source,
                    "to_head_sha": checkpoint_parent,
                    "to_protected_base_sha": protected,
                    "to_route_plan_sha256": route_sha256,
                }],
                "parent_digest": "4" * 64,
                "parent_file_sha256": "5" * 64,
                "protected_base_sha": protected,
                "route_plan_sha256": route_sha256,
                "ticket_blob": checkpoint_parent_ticket_blob,
            })
            self.sign_passport(passport_path, secret, value)

        manifest = {
            "factory_sha": candidate,
            "mode": "successor",
            "source_factory_sha": source,
            "tickets": [ticket],
        }
        write_source_passport(base)
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError,
            "successor qualification requires every selected ticket",
        ):
            ENVIRONMENT.validate_successor_upgrade_cohort(
                self.factory, self.product, controller, "relay", source,
                base, manifest,
            )

        authorization = (
            self.product / "factory/migrations/inflight-release"
            / f"{candidate}.json"
        )
        authorization.parent.mkdir(parents=True)

        def authorize(head: str) -> str:
            authorization.write_text(json.dumps({
                "repository": "example/product",
                "schema": (
                    "nysa.software-factory.inflight-release-authorization/v1"
                ),
                "source_kit_sha": source,
                "target_kit_sha": candidate,
                "tickets": [{
                    "branch": f"ticket/{ticket}", "head": head,
                    "state": "Ready", "ticket": ticket,
                }],
            }, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            run(self.product, "git", "add", str(authorization))
            run(self.product, "git", "commit", "-qm", "authorize checkpoint")
            return run(self.product, "git", "rev-parse", "HEAD")

        protected = authorize(checkpoint)
        write_source_passport(protected)
        ENVIRONMENT.validate_successor_upgrade_cohort(
            self.factory, self.product, controller, "relay", source,
            base, manifest,
        )
        authorization.write_text(
            authorization.read_text().replace(checkpoint, base),
            encoding="utf-8",
        )
        run(self.product, "git", "add", str(authorization))
        run(self.product, "git", "commit", "-qm", "change checkpoint")
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError,
            "successor qualification requires every selected ticket",
        ):
            ENVIRONMENT.validate_successor_upgrade_cohort(
                self.factory, self.product, controller, "relay", source,
                base, manifest,
            )

        run(self.product, "git", "switch", "-q", f"ticket/{ticket}")
        route.write_text('{"partial":"migration"}\n', encoding="utf-8")
        run(self.product, "git", "add", str(route))
        run(self.product, "git", "commit", "-qm", "partial route migration")
        partial_checkpoint = run(self.product, "git", "rev-parse", "HEAD")
        run(self.product, "git", "switch", "-q", "main")
        partial_protected = authorize(partial_checkpoint)
        write_source_passport(partial_protected)

        calls: list[tuple[str, str]] = []

        def verify_partial(
            _product: Path, protected_sha: str, target_sha: str,
            _ticket: str, _branch: str, _head: str,
        ) -> str:
            calls.append((protected_sha, target_sha))
            return "exact" if target_sha == candidate else "replay"

        with mock.patch.object(
            ENVIRONMENT, "verify_inflight_migration", side_effect=verify_partial,
        ):
            ENVIRONMENT.validate_successor_upgrade_cohort(
                self.factory, self.product, controller, "relay", source,
                base, manifest,
            )
        self.assertEqual(calls, [
            (base, source), (partial_protected, candidate),
        ])
        calls.clear()
        with mock.patch.object(
            ENVIRONMENT, "verify_inflight_migration", side_effect=verify_partial,
        ):
            ENVIRONMENT.validate_successor_upgrade_cohort(
                self.factory, self.product, controller, "relay", "6" * 40,
                partial_protected, manifest,
            )
        self.assertEqual(calls, [
            (partial_protected, source), (partial_protected, candidate),
        ])

        value = json.loads(authorization.read_text(encoding="utf-8"))
        value["schema"] = (
            "nysa.software-factory.inflight-release-authorization/v2"
        )
        value["tickets"][0]["source_kit_sha"] = source
        authorization.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        run(self.product, "git", "add", str(authorization))
        run(self.product, "git", "commit", "-qm", "authorize active checkpoint")
        active_good = run(self.product, "git", "rev-parse", "HEAD")
        value["tickets"][0]["state"] = "Building"
        authorization.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        run(self.product, "git", "add", str(authorization))
        run(self.product, "git", "commit", "-qm", "drift active checkpoint")
        active_bad = run(self.product, "git", "rev-parse", "HEAD")
        value["tickets"][0]["state"] = "Ready"
        authorization.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        run(self.product, "git", "add", str(authorization))
        run(self.product, "git", "commit", "-qm", "restore active checkpoint")

        next_candidate = "d" * 40
        next_authorization = authorization.with_name(f"{next_candidate}.json")
        next_value = dict(value, source_kit_sha=candidate,
                          target_kit_sha=next_candidate)
        next_authorization.write_text(
            json.dumps(next_value, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        run(self.product, "git", "add", str(next_authorization))
        run(self.product, "git", "commit", "-qm", "carry checkpoint forward")
        next_manifest = dict(
            manifest, factory_sha=next_candidate,
            source_factory_sha=candidate,
        )
        source_manifest = dict(
            manifest, factory_sha=candidate, source_factory_sha=source,
        )
        write_source_passport(active_good)
        with mock.patch.object(
            ENVIRONMENT, "validate_qualification_manifest",
            return_value=source_manifest,
        ), mock.patch.object(
            ENVIRONMENT, "verify_inflight_migration",
            side_effect=lambda _product, _protected, target, *_args: (
                "exact" if target == next_candidate else "replay"
            ),
        ):
            ENVIRONMENT.validate_successor_upgrade_cohort(
                self.factory, self.product, controller, "relay", candidate,
                active_good, next_manifest,
            )
            with self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError,
                "successor qualification requires every selected ticket",
            ):
                ENVIRONMENT.validate_successor_upgrade_cohort(
                    self.factory, self.product, controller, "relay", candidate,
                    active_bad, next_manifest,
                )

        value = json.loads(authorization.read_text(encoding="utf-8"))
        value["schema"] = (
            "nysa.software-factory.inflight-release-authorization/v2"
        )
        value["tickets"][0]["source_kit_sha"] = "0" * 40
        authorization.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        run(self.product, "git", "add", str(authorization))
        run(self.product, "git", "commit", "-qm", "foreign checkpoint source")
        foreign_protected = run(self.product, "git", "rev-parse", "HEAD")
        write_source_passport(foreign_protected)
        with mock.patch.object(
            ENVIRONMENT, "verify_inflight_migration", side_effect=verify_partial,
        ), self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError,
            "successor qualification requires every selected ticket",
        ):
            ENVIRONMENT.validate_successor_upgrade_cohort(
                self.factory, self.product, controller, "relay", source,
                base, manifest,
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

    def test_repeated_successor_gap_uses_authenticated_edge_release(self) -> None:
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
        source = "863e38a68235dd682c56ca44ee079797f626413a"

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

        controller = (self.workspace / "repeat-successor-controller").resolve()
        passports = controller / "passports"
        passports.mkdir(mode=0o700, parents=True)
        controller.chmod(0o700)
        secret = b"p" * 32
        key = controller / "passport.key"
        key.write_bytes(secret)
        key.chmod(0o600)
        successor = "cbc8bd12fa16af3f6a5872a3b4742f5be5906a8e"
        candidate = "481a958642db2382b96510d637ea50ed5384e047"
        for ticket in ("T-102", "T-103"):
            self.write_passport(
                passports / f"{ticket}.json", secret, ticket, successor,
            )
        path = passports / "T-101.json"
        self.write_passport(path, secret, "T-101", successor, source)
        value = json.loads(path.read_text(encoding="utf-8"))
        value.pop("authentication_sha256")
        value.pop("passport_sha256")

        def migration(
            from_factory: str, to_factory: str, from_head: str, to_head: str,
            to_base: str = "9" * 40, to_route: str = "5" * 64,
        ) -> dict[str, str]:
            return {
                "from_factory_sha": from_factory,
                "from_head_sha": from_head,
                "from_passport_file_sha256": "2" * 64,
                "from_passport_sha256": "3" * 64,
                "from_protected_base_sha": "9" * 40,
                "from_route_plan_sha256": "5" * 64,
                "schema": "nysa.software-factory.ticket-passport-migration/v2",
                "to_factory_sha": to_factory,
                "to_head_sha": to_head,
                "to_protected_base_sha": to_base,
                "to_route_plan_sha256": to_route,
            }

        value.update({
            "charge_records": charges,
            "completed_role_evidence": completed,
            "cumulative_charges_micro_usd": 2,
            "factory_release_history": [
                {"contract_version": "1.8.0", "factory_sha": source},
                {"contract_version": "1.8.0", "factory_sha": successor},
            ],
            "head_sha": end,
            "migration_history": [
                migration(source, source, base, base),
                migration(source, source, end, end),
                migration(
                    source, successor, end, end,
                    "7" * 40, "8" * 64,
                ),
            ],
        })
        self.sign_passport(path, secret, value)
        manifest = {
            "factory_sha": candidate,
            "mode": "successor",
            "source_factory_sha": successor,
            "tickets": ["T-101", "T-102", "T-103"],
        }
        ENVIRONMENT.validate_successor_upgrade_cohort(
            self.factory, self.product, controller, "relay", successor,
            run(self.product, "git", "rev-parse", "HEAD"), manifest,
        )
        value = json.loads(path.read_text(encoding="utf-8"))
        value.pop("authentication_sha256")
        value.pop("passport_sha256")
        value["charge_records"] = [
            {**item, "factory_sha": successor} for item in charges
        ]
        value["completed_role_evidence"] = [
            {**item, "factory_sha": successor} for item in completed
        ]
        self.sign_passport(path, secret, value)
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError,
            "T-101: successor qualification requires every selected ticket",
        ):
            ENVIRONMENT.validate_successor_upgrade_cohort(
                self.factory, self.product, controller, "relay", successor,
                run(self.product, "git", "rev-parse", "HEAD"), manifest,
            )

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
                ENVIRONMENT, "initialize_selected_operator",
                side_effect=AssertionError("operator initialization must not run"),
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
                ENVIRONMENT, "initialize_selected_operator",
                side_effect=AssertionError("operator initialization must not run"),
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
                path = Path(base) / name
                if not path.is_symlink():
                    path.chmod(0o600)
            for name in directories:
                path = Path(base) / name
                if not path.is_symlink():
                    path.chmod(0o700)
        shutil.rmtree(self.root)
        original_write = ENVIRONMENT.write_exact
        crashed = False

        def crash_before_active(path, value):
            nonlocal crashed
            original_write(path, value)
            if not crashed and path.name == "environment.json":
                crashed = True
                raise ENVIRONMENT.EnvironmentError("simulated response loss")

        with (
            mock.patch.object(
                ENVIRONMENT, "write_exact", side_effect=crash_before_active,
            ),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "simulated response loss",
            ),
        ):
            ENVIRONMENT.prepare(argparse.Namespace(
                **vars(args), restore=True,
            ))
        self.assertTrue((self.root / "environment.json").is_file())
        self.assertFalse((self.root / "projects/relay/active.json").exists())
        with mock.patch.object(
            ENVIRONMENT, "initialize_selected_operator",
            wraps=ENVIRONMENT.initialize_selected_operator,
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
