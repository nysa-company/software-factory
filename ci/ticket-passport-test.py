#!/usr/bin/env python3
"""Focused authenticated passport continuity tests."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, wait
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


STATE = module("state_machine", ROOT / "scripts/state-machine.py")
PASSPORT = module("ticket_passport", ROOT / "scripts/ticket-passport.py")
ROLE_OUTPUT = module("role_output", ROOT / "scripts/lib/role_output.py")
ROUTER = module("passport_test_router", ROOT / "scripts/model-router.py")
MANAGER = module("passport_test_manager", ROOT / "scripts/model-manager.py")


def run(*command: str, cwd: Path) -> str:
    return subprocess.run(
        command, cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


class TicketPassportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        os.chmod(self.root, 0o700)
        self.remote = self.root / "remote.git"
        run("git", "init", "--bare", "-q", str(self.remote), cwd=self.root)
        self.product = self.root / "product"
        (self.product / "factory/tickets").mkdir(parents=True)
        (self.product / "factory/route-plans").mkdir()
        (self.product / "factory/runs").mkdir()
        (self.product / "factory/PROJECT.env").write_text(
            'GH_REPO=nysa-company/relay-factory\nTEST_PATHS="app/tests/"\n',
            encoding="utf-8",
        )
        (self.product / "factory/tickets/T-110.md").write_text(
            "# T-110\n\nState: Planning\n", encoding="utf-8"
        )
        (self.product / "factory/route-plans/T-110.json").write_text(
            f'{{"kit_sha":"{"a" * 40}","ticket":"T-110"}}\n',
            encoding="utf-8",
        )
        (self.product / ".gitignore").write_text(
            "factory/runs/\n", encoding="utf-8"
        )
        run("git", "init", "-q", "-b", "ticket/T-110", cwd=self.product)
        run("git", "config", "user.name", "Test", cwd=self.product)
        run("git", "config", "user.email", "test@example.invalid", cwd=self.product)
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "seed", cwd=self.product)
        run("git", "remote", "add", "origin", str(self.remote), cwd=self.product)
        run("git", "push", "-qu", "origin", "HEAD:main", cwd=self.product)
        run("git", "symbolic-ref", "HEAD", "refs/heads/main", cwd=self.remote)
        self.state_dir = STATE.safe_state_dir(self.root / "controller")
        self.state_args = argparse.Namespace(
            contract_version="1.8.0",
            factory_root=self.product,
            factory_sha="a" * 40,
            kit_dir=ROOT,
            lease="",
            project="relay",
            receipt="",
            require_used=False,
            role="planner",
            state_dir=self.state_dir,
            ticket="T-110",
            workdir=self.product,
        )
        self.passport_args = argparse.Namespace(
            action="export",
            contract_version="1.8.0",
            factory_root=self.product,
            factory_sha="a" * 40,
            project="relay",
            publication_state="none",
            receipt="",
            state_dir=self.state_dir,
            ticket="T-110",
            workdir=self.product,
        )
        self.origin = mock.patch.dict(
            os.environ, {"FACTORY_CERTIFIED_PRODUCT_ORIGIN": str(self.remote)}
        )
        self.origin.start()

    def tearDown(self) -> None:
        self.origin.stop()
        self.temporary.cleanup()

    def test_concurrent_key_initialization_never_exposes_partial_key(self) -> None:
        started = __import__("threading").Event()
        release = __import__("threading").Event()
        token_bytes = PASSPORT.secrets.token_bytes

        def paused_token_bytes(size: int) -> bytes:
            started.set()
            self.assertTrue(release.wait(2))
            return token_bytes(size)

        with mock.patch.object(
            PASSPORT.secrets, "token_bytes", side_effect=paused_token_bytes
        ), ThreadPoolExecutor(max_workers=2) as pool:
            creator = pool.submit(PASSPORT.key, self.state_dir)
            self.assertTrue(started.wait(2))
            reader = pool.submit(PASSPORT.key, self.state_dir)
            self.assertFalse(wait([reader], timeout=0.05).done)
            release.set()
            self.assertEqual(creator.result(timeout=2), reader.result(timeout=2))

    def terminal(
        self,
        run_id: str,
        role: str,
        receipt: str,
        factory_sha: str,
        content: bytes | None = None,
    ) -> None:
        output_path = self.product / f"factory/runs/{run_id}.out"
        published = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/lib/role_output.py"),
                "publish",
                str(output_path),
            ],
            input=content if content is not None else f"{role} output\n".encode(),
            capture_output=True,
            check=True,
        )
        output = published.stdout.decode().strip()
        (self.product / f"factory/runs/{run_id}.meta").write_text(
            f"run_id={run_id}\n"
            "phase=completed\n"
            "accounting_state=completed\n"
            "task_submitted=1\n"
            "effective_cost=1.500000\n"
            "exit_status=0\n"
            "ticket=T-110\n"
            f"role={role}\n"
            "role_exit=ok\n"
            f"role_head_before={run('git', 'rev-parse', 'HEAD', cwd=self.product)}\n"
            f"kit_sha={factory_sha}\n"
            "contract_version=1.8.0\n"
            f"transition_receipt_sha256={receipt}\n"
            f"output_sha256={output}\n",
            encoding="utf-8",
        )

    def converged_success_terminal(
        self, run_id: str, receipt: str, head_before: str,
        adapter: str = "cursor-openai",
    ) -> None:
        output_path = self.product / f"factory/runs/{run_id}.out"
        output_digest = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/lib/role_output.py"),
                "publish",
                str(output_path),
            ],
            input=b'{"subtype":"success","type":"result"}\n',
            capture_output=True,
            check=True,
        ).stdout.decode().strip()
        progress = self.product / f"factory/runs/{run_id}.progress.jsonl"
        records = [
            {
                "event_sha256": "1" * 64,
                "observed_monotonic_ns": 1,
                "sequence": 1,
                "subtype": "init",
                "type": "system",
            },
            {
                "event_sha256": "2" * 64,
                "observed_monotonic_ns": 2,
                "sequence": 2,
                "subtype": "success",
                "type": "result",
            },
        ]
        progress.write_text(
            "".join(
                json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n"
                for item in records
            ),
            encoding="utf-8",
        )
        os.chmod(progress, 0o600)
        (self.product / f"factory/runs/{run_id}.meta").write_text(
            f"run_id={run_id}\n"
            "phase=abandoned\n"
            "accounting_state=abandoned_conservative\n"
            "reserved_usd=10.00\n"
            "go_issued=1\n"
            "task_submitted=1\n"
            "effective_cost=10.00\n"
            "exit_status=128\n"
            "cost_basis=conservative_reservation\n"
            "ticket=T-110\n"
            "role=builder\n"
            f"adapter={adapter}\n"
            "provider_attempt_id=attempt-1\n"
            "role_exit=\n"
            "role_branch_before=ticket/T-110\n"
            f"role_head_before={head_before}\n"
            f"kit_sha={'a' * 40}\n"
            "contract_version=1.8.0\n"
            f"transition_receipt_sha256={receipt}\n"
            f"output_sha256={output_digest}\n"
            "progress_events=\n"
            "progress_journal_sha256=\n"
            "terminal_reason_code=\n",
            encoding="utf-8",
        )

    def model_identity_success_terminal(
        self, run_id: str, receipt: str, head_before: str,
    ) -> None:
        output_path = self.product / f"factory/runs/{run_id}.out"
        output_digest = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/lib/role_output.py"),
                "publish",
                str(output_path),
            ],
            input=(
                b'{"model":"Opus 5 300K Medium","subtype":"init",'
                b'"type":"system"}\n'
                b'{"subtype":"success","type":"result"}\n'
                b'cursor reported unapproved model: Opus 5 300K Medium\n'
                b'Cursor output validation/redaction failed\n'
            ),
            capture_output=True,
            check=True,
        ).stdout.decode().strip()
        progress = self.product / f"factory/runs/{run_id}.progress.jsonl"
        records = [
            {
                "event_sha256": "1" * 64,
                "observed_monotonic_ns": 1,
                "sequence": 1,
                "subtype": "init",
                "type": "system",
            },
            {
                "event_sha256": "2" * 64,
                "observed_monotonic_ns": 2,
                "sequence": 2,
                "subtype": "success",
                "type": "result",
            },
        ]
        progress.write_text(
            "".join(
                json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n"
                for item in records
            ),
            encoding="utf-8",
        )
        os.chmod(progress, 0o600)
        (self.product / f"factory/runs/{run_id}.meta").write_text(
            f"run_id={run_id}\n"
            "phase=completed\n"
            "accounting_state=abandoned_conservative\n"
            "reserved_usd=2.00\n"
            "go_issued=1\n"
            "task_submitted=1\n"
            "turns=3\n"
            "effective_cost=2.00\n"
            "exit_status=9\n"
            "cost_basis=conservative_reservation\n"
            "ticket=T-110\n"
            "role=spec-linter\n"
            "adapter=cursor-anthropic\n"
            "route_id=cursor-claude-opus-5-thinking-medium\n"
            f"route_plan_sha256={hashlib.sha256((self.product / 'factory/route-plans/T-110.json').read_bytes()).hexdigest()}\n"
            "provider_attempt_id=attempt-identity\n"
            "role_exit=provider_failed\n"
            "role_branch_before=ticket/T-110\n"
            f"role_head_before={head_before}\n"
            f"kit_sha={'a' * 40}\n"
            "contract_version=1.8.0\n"
            f"transition_receipt_sha256={receipt}\n"
            f"output_sha256={output_digest}\n"
            "progress_events=2\n"
            f"progress_journal_sha256={hashlib.sha256(progress.read_bytes()).hexdigest()}\n"
            "terminal_reason_code=\n",
            encoding="utf-8",
        )

    def direct_model_identity_terminal(
        self, run_id: str, receipt: str, head_before: str,
        role: str = "planner", policy_hash: str = "9" * 64,
    ) -> None:
        plan = json.loads(
            (self.product / "factory/route-plans/T-110.json").read_text()
        )
        selected = PASSPORT.route_selection(plan, role)
        _catalog, routes, _profiles, _profile_map = ROUTER.load_policy()
        route = routes[selected["route_id"]]
        actual = {
            "gpt-5.6-sol-high": "GPT-5.6 Sol 1M High",
            "claude-opus-5-thinking-medium": "Opus 5 300K Medium",
        }.get(route["selection_id"], route["expected_reported_identity"])
        adapter = route["adapter"]
        output_path = self.product / f"factory/runs/{run_id}.out"
        result = "APPROVE" if role == "reviewer" else "completed"
        output_digest = subprocess.run(
            [sys.executable, str(ROOT / "scripts/lib/role_output.py"),
             "publish", str(output_path)],
            input=(
                json.dumps({"model": actual, "subtype": "init", "type": "system"})
                + "\n"
                + json.dumps({
                    "result": result, "subtype": "success", "type": "result",
                })
                + "\n"
                + f"cursor reported unapproved model: {actual}\n"
                + "Cursor output validation/redaction failed\n"
            ).encode(),
            capture_output=True, check=True,
        ).stdout.decode().strip()
        progress = self.product / f"factory/runs/{run_id}.progress.jsonl"
        progress.write_text("".join(
            json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n"
            for item in ({
                "event_sha256": "1" * 64, "observed_monotonic_ns": 1,
                "sequence": 1, "subtype": "init", "type": "system",
            }, {
                "event_sha256": "2" * 64, "observed_monotonic_ns": 2,
                "sequence": 2, "subtype": "success", "type": "result",
            })
        ), encoding="utf-8")
        os.chmod(progress, 0o600)
        route_digest = hashlib.sha256(
            (self.product / "factory/route-plans/T-110.json").read_bytes()
        ).hexdigest()
        (self.product / f"factory/runs/{run_id}.meta").write_text(
            f"run_id={run_id}\n"
            "phase=completed\naccounting_state=abandoned_conservative\n"
            "reserved_usd=2.00\ngo_issued=1\ntask_submitted=1\nturns=3\n"
            "effective_cost=2.00\nexit_status=9\n"
            "cost_basis=conservative_reservation\nticket=T-110\n"
            f"role={role}\nadapter={adapter}\n"
            f"provider_family={route['provider_family']}\n"
            f"model_id={route['selection_id']}\neffort={selected['effort']}\n"
            "selection_reason=pinned_route_plan\nadapter_version=1.0.0\n"
            f"route_id={route['route_id']}\ngateway_id={route['gateway_id']}\n"
            f"inference_provider_id={route['inference_provider_id']}\n"
            f"account_route_id={route['account_route_id']}\n"
            f"transport={route['transport']}\n"
            f"policy_hash={policy_hash}\nroute_plan_sha256={route_digest}\n"
            "provider_attempt_id=attempt-direct\nrole_exit=provider_failed\n"
            "role_branch_before=ticket/T-110\n"
            f"role_head_before={head_before}\nrole_remote_before={head_before}\n"
            f"kit_sha={'a' * 40}\ncontract_version=1.8.0\n"
            f"transition_receipt_sha256={receipt}\n"
            f"output_sha256={output_digest}\nprogress_events=2\n"
            f"progress_journal_sha256={hashlib.sha256(progress.read_bytes()).hexdigest()}\n"
            "terminal_reason_code=\n",
            encoding="utf-8",
        )

    def test_passportless_cursor_planner_success_is_recovered_once(self) -> None:
        catalog, routes, _profiles, profile_map = ROUTER.load_policy()
        readiness = {
            route_id: {
                "adapter_version": "1.0.0", "reason": "ok",
                "reported_identity": value["expected_reported_identity"],
                "state": "READY",
            }
            for route_id, value in routes.items() if value["enabled"]
        }
        resolution = ROUTER.resolve_policy(
            catalog, routes, profile_map["cursor-opus-v1"], readiness,
        )
        route = resolution["selections"]["planner"]
        plan = {
            "created_at": "2026-08-07T00:00:00Z",
            "kit_sha": "a" * 40,
            "resolution": resolution,
            "schema": "ticket-model-route-plan/v1",
            "ticket": "T-110",
        }
        (self.product / "factory/route-plans/T-110.json").write_text(
            json.dumps(plan, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        (self.product / "factory/tickets/T-110.md").write_text(
            f"# T-110\n\nState: Planning\n\nKit-SHA: {'a' * 40}\n\n## Log\n",
            encoding="utf-8",
        )
        run("git", "add", "factory", cwd=self.product)
        run("git", "commit", "-qm", "pin Cursor Planner", cwd=self.product)
        run("git", "push", "-q", "origin", "HEAD:ticket/T-110", cwd=self.product)
        issued = STATE.issue(self.state_args, "RUN planner")
        self.state_args.receipt = issued["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        input_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            ticket.read_text(encoding="utf-8").replace(
                "## Log", "Plan: retain completed work\n\n## Log"
            ), encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "record Planner result", cwd=self.product)
        output_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        run_id = "run-passportless-model-success"
        self.direct_model_identity_terminal(
            run_id, issued["receipt_sha256"], input_head,
            policy_hash=resolution["policy_hash"],
        )
        self.passport_args.receipt = issued["receipt_sha256"]
        self.passport_args.run_id = run_id
        secret = PASSPORT.key(self.state_dir)
        same_release = PASSPORT.direct_model_identity_evidence(
            self.passport_args, PASSPORT.identity(self.passport_args)
        )
        self.assertEqual(same_release["topology"]["control_commit_count"], 0)

        plan = MANAGER.migrate_v1_plan(
            (ROUTER.canonical_json(plan) + "\n").encode(), output_head,
            "b" * 40, "2026-08-07T00:01:00Z", catalog, routes, profile_map,
        )
        (self.product / "factory/route-plans/T-110.json").write_text(
            ROUTER.canonical_json(plan) + "\n",
            encoding="utf-8",
        )
        ticket.write_text(
            ticket.read_text(encoding="utf-8").replace("a" * 40, "b" * 40),
            encoding="utf-8",
        )
        route_path = self.product / "factory/route-plans/T-110.json"
        valid_route = route_path.read_text()
        corrupt = json.loads(valid_route)
        corrupt["revisions"][0]["revision_hash"] = "f" * 64
        route_path.write_text(ROUTER.canonical_json(corrupt) + "\n")
        route_evidence = module(
            "passport_test_route_evidence_invalid",
            ROOT / "scripts/lib/route_evidence.py",
        )
        with self.assertRaises(route_evidence.RouteEvidenceError):
            route_evidence.validate_route(
                self.product, self.product, "T-110", "b" * 40,
            )
        route_path.write_text(valid_route)
        run("git", "add", "factory", cwd=self.product)
        run("git", "commit", "-qm", "migrate recovered route", cwd=self.product)
        route_evidence = module(
            "passport_test_route_evidence", ROOT / "scripts/lib/route_evidence.py"
        )
        self.assertTrue(route_evidence.journal_extends(
            subprocess.run(
                ["git", "-C", str(self.product), "show", f"{output_head}:factory/route-plans/T-110.json"],
                capture_output=True, check=True,
            ).stdout,
            (self.product / "factory/route-plans/T-110.json").read_bytes(),
        ))
        self.assertTrue(route_evidence.exact_kit_sha_change(
            subprocess.run(
                ["git", "-C", str(self.product), "show", f"{output_head}:factory/tickets/T-110.md"],
                capture_output=True, check=True,
            ).stdout,
            ticket.read_bytes(),
        ))
        self.assertEqual(
            re.findall(rb"^Kit-SHA:\s*([0-9a-f]{40})\s*$", ticket.read_bytes(), re.M),
            [b"b" * 40],
        )
        migration_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        self.assertEqual(
            run("git", "ls-tree", migration_head, "factory/tickets/T-110.md", cwd=self.product).split()[0],
            "100644",
        )
        self.passport_args.factory_sha = "b" * 40
        recovered = PASSPORT.recover_model_identity_success(
            self.passport_args, secret
        )
        self.assertNotEqual(recovered["head_sha"], output_head)
        self.assertEqual(recovered["current_stage"], "RUN planner")
        self.assertEqual(len(recovered["charge_records"]), 1)
        self.assertEqual(
            recovered["completed_role_evidence"][-1]["role"], "planner"
        )
        correction = recovered["completed_role_corrections"][-1]
        self.assertEqual(
            correction["schema"], PASSPORT.PASSPORTLESS_MODEL_CORRECTION_SCHEMA
        )
        self.assertIsNone(correction["receipt_parent_file_sha256"])
        self.assertEqual(correction["failed_factory_sha"], "a" * 40)
        self.assertEqual(correction["recovery_factory_sha"], "b" * 40)
        replayed = PASSPORT.recover_model_identity_success(
            self.passport_args, secret
        )
        self.assertEqual(replayed["passport_sha256"], recovered["passport_sha256"])

        (self.product / "untrusted.txt").write_text("extra\n", encoding="utf-8")
        run("git", "add", "untrusted.txt", cwd=self.product)
        run("git", "commit", "-qm", "add untrusted descendant", cwd=self.product)
        with self.assertRaisesRegex(PASSPORT.PassportError, "role output"):
            PASSPORT.recover_model_identity_success(self.passport_args, secret)

    def test_direct_model_identity_evidence_covers_all_roles_and_releases(self) -> None:
        catalog, routes, _profiles, profile_map = ROUTER.load_policy()
        readiness = {
            route_id: {
                "adapter_version": "1.0.0", "reason": "ok",
                "reported_identity": value["expected_reported_identity"],
                "state": "READY",
            }
            for route_id, value in routes.items() if value["enabled"]
        }
        resolution = ROUTER.resolve_policy(
            catalog, routes, profile_map["cursor-opus-v1"], readiness,
        )
        seed = run("git", "rev-parse", "HEAD", cwd=self.product)
        observed = set()
        for role in ("planner", "test-author", "builder", "reviewer", "narrator"):
            for migrated in (False, True):
                with self.subTest(role=role, migrated=migrated):
                    run("git", "reset", "--hard", seed, cwd=self.product)
                    shutil.rmtree(self.product / "factory/runs")
                    (self.product / "factory/runs").mkdir()
                    (self.state_dir / "T-110.json").unlink(missing_ok=True)
                    plan = {
                        "created_at": "2026-08-07T00:00:00Z",
                        "kit_sha": "a" * 40,
                        "resolution": resolution,
                        "schema": "ticket-model-route-plan/v1",
                        "ticket": "T-110",
                    }
                    route_path = self.product / "factory/route-plans/T-110.json"
                    route_path.write_text(ROUTER.canonical_json(plan) + "\n")
                    ticket = self.product / "factory/tickets/T-110.md"
                    ticket.write_text(
                        f"# T-110\n\nState: Planning\nKit-SHA: {'a' * 40}\n"
                    )
                    run("git", "add", "factory", cwd=self.product)
                    run("git", "commit", "-qm", f"pin {role}", cwd=self.product)
                    input_head = run("git", "rev-parse", "HEAD", cwd=self.product)
                    selected = resolution["selections"][role]
                    observed.add(selected["adapter"])
                    receipt = {
                        **PASSPORT.identity(self.passport_args),
                        "consumed": True,
                        "contract_version": "1.8.0",
                        "factory_sha": "a" * 40,
                        "passport_sha256": None if role == "planner" else "8" * 64,
                        "role": role,
                        "route_plan_sha256": hashlib.sha256(route_path.read_bytes()).hexdigest(),
                        "schema": PASSPORT.RECEIPT_SCHEMA,
                        "stage": f"RUN {role}",
                    }
                    receipt["receipt_sha256"] = hashlib.sha256(
                        PASSPORT.canonical({
                            key: value for key, value in receipt.items()
                            if key not in {"consumed", "consumed_at_epoch", "receipt_sha256"}
                        })
                    ).hexdigest()
                    PASSPORT.write_atomic(self.state_dir / "T-110.json", receipt)
                    if role != "reviewer":
                        ticket.write_text(
                            ticket.read_text() + f"\nRole-Result: {role} complete\n"
                        )
                        run("git", "add", str(ticket), cwd=self.product)
                        run("git", "commit", "-qm", f"record {role}", cwd=self.product)
                    run_id = f"matrix-{role}-{int(migrated)}"
                    self.direct_model_identity_terminal(
                        run_id, receipt["receipt_sha256"], input_head,
                        role=role, policy_hash=resolution["policy_hash"],
                    )
                    self.passport_args.receipt = receipt["receipt_sha256"]
                    self.passport_args.run_id = run_id
                    if migrated:
                        journal = MANAGER.migrate_v1_plan(
                            route_path.read_bytes(), input_head, "b" * 40,
                            "2026-08-07T00:01:00Z", catalog, routes, profile_map,
                        )
                        route_path.write_text(ROUTER.canonical_json(journal) + "\n")
                        ticket.write_text(ticket.read_text().replace("a" * 40, "b" * 40))
                        run("git", "add", "factory", cwd=self.product)
                        run("git", "commit", "-qm", "migrate route", cwd=self.product)
                        self.passport_args.factory_sha = "b" * 40
                    else:
                        self.passport_args.factory_sha = "a" * 40
                    evidence = PASSPORT.direct_model_identity_evidence(
                        self.passport_args, PASSPORT.identity(self.passport_args)
                    )
                    self.assertEqual(evidence["terminal"]["role"], role)
                    self.assertEqual(
                        evidence["topology"]["control_commit_count"],
                        1 if migrated else 0,
                    )
        self.assertEqual(observed, {"cursor-anthropic", "cursor-openai"})
        manifest = self.product / "factory/runs/matrix-narrator-1.meta"
        original = manifest.read_text()
        for field, wrong in (
            ("provider_family", "anthropic"),
            ("model_id", "gpt-5.6-terra-medium"),
        ):
            with self.subTest(wrong_field=field):
                manifest.write_text(re.sub(
                    rf"(?m)^{field}=.*$", f"{field}={wrong}", original,
                ))
                with self.assertRaisesRegex(
                    PASSPORT.PassportError, "typed model-identity",
                ):
                    PASSPORT.direct_model_identity_evidence(
                        self.passport_args, PASSPORT.identity(self.passport_args)
                    )
        manifest.write_text(original)

    def test_model_identity_recovery_preserves_migrated_passport_roles(self) -> None:
        catalog, routes, _profiles, profile_map = ROUTER.load_policy()
        readiness = {
            route_id: {
                "adapter_version": "1.0.0", "reason": "ok",
                "reported_identity": value["expected_reported_identity"],
                "state": "READY",
            }
            for route_id, value in routes.items() if value["enabled"]
        }
        resolution = ROUTER.resolve_policy(
            catalog, routes, profile_map["cursor-opus-v1"], readiness,
        )
        seed = run("git", "rev-parse", "HEAD", cwd=self.product)
        observed = set()
        for role in ("test-author", "builder", "reviewer", "narrator"):
            for migrated in (False, True):
                with self.subTest(role=role, migrated=migrated):
                    run("git", "reset", "--hard", seed, cwd=self.product)
                    shutil.rmtree(self.product / "factory/runs")
                    (self.product / "factory/runs").mkdir()
                    state_dir = STATE.safe_state_dir(
                        self.root / f"controller-{role}-{int(migrated)}"
                    )
                    self.state_args.state_dir = state_dir
                    self.state_args.factory_sha = "a" * 40
                    self.state_args.role = "planner"
                    self.passport_args.state_dir = state_dir
                    self.passport_args.factory_sha = "a" * 40
                    plan = {
                        "created_at": "2026-08-07T00:00:00Z",
                        "kit_sha": "a" * 40,
                        "resolution": resolution,
                        "schema": "ticket-model-route-plan/v1",
                        "ticket": "T-110",
                    }
                    route_path = self.product / "factory/route-plans/T-110.json"
                    route_path.write_text(ROUTER.canonical_json(plan) + "\n")
                    ticket = self.product / "factory/tickets/T-110.md"
                    ticket.write_text(
                        f"# T-110\n\nState: Planning\nKit-SHA: {'a' * 40}\n"
                    )
                    run("git", "add", "factory", cwd=self.product)
                    run("git", "commit", "-qm", f"pin {role}", cwd=self.product)
                    secret = PASSPORT.key(state_dir)

                    planner = STATE.issue(self.state_args, "RUN planner")
                    self.state_args.receipt = planner["receipt_sha256"]
                    STATE.verify(self.state_args, consume=True)
                    self.terminal(
                        f"prior-{role}-{int(migrated)}", "planner",
                        planner["receipt_sha256"], "a" * 40,
                    )
                    self.passport_args.receipt = planner["receipt_sha256"]
                    prior = PASSPORT.export(self.passport_args, secret)

                    self.state_args.role = role
                    issued = STATE.issue(self.state_args, f"RUN {role}")
                    self.state_args.receipt = issued["receipt_sha256"]
                    STATE.verify(self.state_args, consume=True)
                    input_head = run("git", "rev-parse", "HEAD", cwd=self.product)
                    if role != "reviewer":
                        ticket.write_text(
                            ticket.read_text() + f"\nRole-Result: {role} complete\n"
                        )
                        run("git", "add", str(ticket), cwd=self.product)
                        run("git", "commit", "-qm", f"record {role}", cwd=self.product)
                    run_id = f"recover-{role}-{int(migrated)}"
                    self.direct_model_identity_terminal(
                        run_id, issued["receipt_sha256"], input_head,
                        role=role, policy_hash=resolution["policy_hash"],
                    )
                    self.passport_args.receipt = issued["receipt_sha256"]
                    self.passport_args.run_id = run_id
                    observed.add(resolution["selections"][role]["adapter"])

                    def migrate_current_passport() -> dict:
                        journal = MANAGER.migrate_v1_plan(
                            route_path.read_bytes(),
                            run("git", "rev-parse", "HEAD", cwd=self.product),
                            "b" * 40, "2026-08-07T00:01:00Z",
                            catalog, routes, profile_map,
                        )
                        route_path.write_text(ROUTER.canonical_json(journal) + "\n")
                        ticket.write_text(
                            ticket.read_text().replace("a" * 40, "b" * 40)
                        )
                        run("git", "add", "factory", cwd=self.product)
                        run("git", "commit", "-qm", "migrate route", cwd=self.product)
                        self.passport_args.factory_sha = "b" * 40
                        return PASSPORT.migrate(self.passport_args, secret)

                    if migrated:
                        prior = migrate_current_passport()
                        if role == "test-author":
                            passport_path = state_dir / "passports/T-110.json"
                            invalid = json.loads(json.dumps({
                                key: value for key, value in prior.items()
                                if key not in {
                                    "authentication_sha256", "passport_sha256",
                                }
                            }))
                            invalid["migration_history"][-1][
                                "from_passport_file_sha256"
                            ] = "0" * 64
                            PASSPORT.write_atomic(
                                passport_path, PASSPORT.authenticate(invalid, secret)
                            )
                            with self.assertRaisesRegex(
                                PASSPORT.PassportError, "passport lineage",
                            ):
                                PASSPORT.recover_model_identity_success(
                                    self.passport_args, secret
                                )
                            PASSPORT.write_atomic(passport_path, prior)

                    artifacts = {
                        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in (self.product / "factory/runs").iterdir()
                    }
                    recovered = PASSPORT.recover_model_identity_success(
                        self.passport_args, secret
                    )
                    replayed = PASSPORT.recover_model_identity_success(
                        self.passport_args, secret
                    )
                    if not migrated:
                        direct = replayed
                        prior = migrate_current_passport()
                        recovered = PASSPORT.recover_model_identity_success(
                            self.passport_args, secret
                        )
                        replayed = PASSPORT.recover_model_identity_success(
                            self.passport_args, secret
                        )
                        self.assertEqual(
                            direct["charge_records"], recovered["charge_records"]
                        )
                        self.assertEqual(
                            direct["completed_role_evidence"],
                            recovered["completed_role_evidence"],
                        )
                        self.assertEqual(
                            direct["completed_role_corrections"],
                            recovered["completed_role_corrections"],
                        )
                    self.assertEqual(
                        replayed["passport_sha256"], recovered["passport_sha256"]
                    )
                    self.assertEqual(
                        artifacts,
                        {
                            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in (self.product / "factory/runs").iterdir()
                        },
                    )
                    self.assertEqual(
                        sum(
                            item["run_id"] == run_id
                            for item in recovered["charge_records"]
                        ),
                        1,
                    )
                    self.assertEqual(
                        sum(
                            item["run_id"] == run_id
                            for item in recovered["completed_role_evidence"]
                        ),
                        1,
                    )
                    self.assertEqual(
                        sum(
                            item["run_id"] == run_id
                            for item in recovered["completed_role_corrections"]
                        ),
                        1,
                    )
                    self.assertEqual(
                        recovered["charge_records"][0], prior["charge_records"][0]
                    )
        self.assertEqual(observed, {"cursor-anthropic", "cursor-openai"})

    def test_role_output_uses_one_streaming_eight_mib_bound(self) -> None:
        existing_size = 5_662_048
        self.terminal(
            "run-existing",
            "planner",
            "e" * 64,
            "a" * 40,
            b"x" * existing_size,
        )
        completed, charges = PASSPORT.run_evidence(
            self.product / "factory", "T-110"
        )
        self.assertEqual(len(completed), 1)
        self.assertEqual(len(charges), 1)
        self.assertEqual(
            (self.product / "factory/runs/run-existing.out").stat().st_size,
            existing_size,
        )
        existing = self.product / "factory/runs/run-existing.out"
        os.chmod(existing, 0o644)
        self.assertEqual(
            len(PASSPORT.run_charges(self.product / "factory", "T-110")), 1
        )
        with self.assertRaisesRegex(ValueError, "unsafe role output"):
            PASSPORT.run_evidence(self.product / "factory", "T-110")
        os.chmod(existing, 0o600)

        refused = self.product / "factory/runs/run-refused.out"
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/lib/role_output.py"),
                "publish",
                str(refused),
            ],
            input=b"x" * (ROLE_OUTPUT.MAX_BYTES + 1),
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 8)
        self.assertIn(b"ROLE_OUTPUT_INVALID", result.stderr)
        self.assertEqual(
            refused.read_text(encoding="utf-8"),
            "ROLE_OUTPUT_INVALID: role output exceeds 8388608-byte limit\n",
        )
        self.assertEqual(
            result.stdout.decode().strip(),
            hashlib.sha256(refused.read_bytes()).hexdigest(),
        )
        symlink_target = self.root / "unrelated"
        symlink_target.write_text("untouched\n", encoding="utf-8")
        replacement = self.product / "factory/runs/replaced.out"
        replacement.symlink_to(symlink_target)
        replaced = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/lib/role_output.py"),
                "publish",
                str(replacement),
            ],
            input=b"wrapper output\n",
            capture_output=True,
            check=True,
        )
        self.assertEqual(symlink_target.read_text(encoding="utf-8"), "untouched\n")
        self.assertFalse(replacement.is_symlink())
        self.assertEqual(replacement.read_bytes(), b"wrapper output\n")
        self.assertEqual(
            replaced.stdout.decode().strip(),
            hashlib.sha256(replacement.read_bytes()).hexdigest(),
        )

        oversized = self.product / "factory/runs/run-existing.out"
        oversized.write_bytes(b"x" * (ROLE_OUTPUT.MAX_BYTES + 1))
        os.chmod(oversized, 0o600)
        with self.assertRaisesRegex(ValueError, "8388608-byte limit"):
            PASSPORT.run_evidence(self.product / "factory", "T-110")

    def test_run_agent_terminalizes_oversized_role_output(self) -> None:
        factory_sha = run("git", "rev-parse", "HEAD", cwd=ROOT)
        release = self.root / "release"
        shutil.copytree(ROOT / "scripts", release / "scripts")
        (release / "integrations/hermes").mkdir(parents=True)
        shutil.copy2(
            ROOT / "integrations/hermes/contract.json",
            release / "integrations/hermes/contract.json",
        )
        adapter = release / "scripts/adapters/mock.sh"
        adapter.write_text(
            "#!/usr/bin/env bash\n"
            "python3 - <<'PY'\n"
            "import sys\n"
            "sys.stdout.buffer.write(b'x' * (8 * 1024 * 1024 + 1))\n"
            "PY\n",
            encoding="utf-8",
        )
        os.chmod(adapter, 0o755)
        release_tree = run(
            "bash",
            "-c",
            'source "$1"; factory_directory_tree "$2"',
            "_",
            str(ROOT / "scripts/lib/kit-pin.sh"),
            str(release),
            cwd=self.root,
        )

        (self.product / "factory/ENVELOPE.env").write_text(
            "PER_RUN_BUDGET_USD=1.00\n"
            "PER_TICKET_BUDGET_USD=20.00\n"
            "PER_RUN_MAX_TURNS=5\n"
            "PER_RUN_TIMEOUT_MIN=1\n"
            "DAILY_CAP_USD=50.00\n",
            encoding="utf-8",
        )
        (self.product / "factory/KIT_PIN").write_text(
            factory_sha + "\n", encoding="utf-8"
        )
        (self.product / "factory/ledger.csv").write_text(
            "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,"
            "exit_status,run_id,provider_family,model_id,selection_reason,"
            "cost_basis,adapter_version\n",
            encoding="utf-8",
        )
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            f"# T-110\n\nState: Ready\n\nKit-SHA: {factory_sha}\n",
            encoding="utf-8",
        )
        (self.product / ".gitignore").write_text(
            "factory/runs/\n"
            "factory/runtime-ledger.csv\n"
            "factory/.active-runs/\n"
            "factory/.launch.lock/\n"
            "factory/.provider.lock/\n"
            "factory/.ledger.lock/\n",
            encoding="utf-8",
        )
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "oversized output fixture", cwd=self.product)
        run(
            "git",
            "push",
            "-qu",
            "origin",
            "HEAD:ticket/T-110",
            cwd=self.product,
        )

        transition_state = STATE.safe_state_dir(self.root / "run-state")
        transition_args = argparse.Namespace(
            contract_version="1.8.0",
            factory_root=self.product,
            factory_sha=factory_sha,
            kit_dir=release,
            lease="",
            project="output-test",
            receipt="",
            require_used=False,
            role="planner",
            state_dir=transition_state,
            ticket="T-110",
            workdir=self.product,
        )
        transition = STATE.issue(transition_args, "RUN planner")
        transition_args.receipt = transition["receipt_sha256"]
        STATE.verify(transition_args, consume=True)

        environment = dict(os.environ)
        environment.update({
            "FACTORY_ADAPTER_OVERRIDE": "mock",
            "FACTORY_CERTIFIED_PRODUCT_ORIGIN": str(self.remote),
            "FACTORY_GLOBAL_ENV": str(self.root / "missing-global.env"),
            "FACTORY_PROJECT": "output-test",
            "FACTORY_RELEASE_CONTRACT_VERSION": "1.8.0",
            "FACTORY_RELEASE_PATH": str(release),
            "FACTORY_RELEASE_SHA": factory_sha,
            "FACTORY_RELEASE_TREE": release_tree,
            "FACTORY_ROOT": str(self.product),
            "FACTORY_TEST_MODE": "1",
            "FACTORY_TRUSTED_TEST_HARNESS": "1",
            "FACTORY_TRANSITION_RECEIPT_SHA256":
                transition["receipt_sha256"],
            "FACTORY_TRANSITION_STATE_DIR": str(transition_state),
        })
        result = subprocess.run(
            [
                str(release / "scripts/run-agent.sh"),
                "--role",
                "planner",
                "--ticket",
                "T-110",
                "--",
                "oversized output",
            ],
            cwd=self.product,
            env=environment,
            capture_output=True,
            check=False,
            # The real oversized-output path hashes, terminalizes, and cleans a
            # multi-megabyte artifact. Keep this harness bound above its
            # measured parallel-suite runtime; production bounds are unchanged.
            timeout=90,
        )
        self.assertEqual(result.returncode, 11, result.stderr.decode())
        self.assertIn(b"ROLE_OUTPUT_INVALID", result.stderr)
        manifests = list((self.product / "factory/runs").glob("*.meta"))
        self.assertEqual(len(manifests), 1)
        fields = PASSPORT.manifest_fields(manifests[0])
        self.assertEqual(fields["accounting_state"], "abandoned_conservative")
        self.assertEqual(fields["effective_cost"], "1.00")
        self.assertEqual(fields["exit_status"], "11")
        self.assertEqual(fields["role_exit"], "role_exit_invalid_output")
        output = manifests[0].with_suffix(".out")
        self.assertEqual(
            fields["output_sha256"],
            hashlib.sha256(output.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            output.read_text(encoding="utf-8"),
            "ROLE_OUTPUT_INVALID: role output exceeds 8388608-byte limit\n",
        )

    def test_passport_chains_receipts_without_replay_or_double_charge(self) -> None:
        secret = PASSPORT.key(self.state_dir)
        first = STATE.issue(self.state_args, "RUN planner")
        self.state_args.receipt = first["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal("run-1", "planner", first["receipt_sha256"], "a" * 40)
        self.passport_args.receipt = first["receipt_sha256"]
        exported = PASSPORT.export(self.passport_args, secret)
        self.assertEqual(exported["cumulative_charges_micro_usd"], 1_500_000)
        self.assertEqual(len(exported["completed_role_evidence"]), 1)

        validated = PASSPORT.validate(self.passport_args, secret)
        self.assertEqual(validated["passport_sha256"], exported["passport_sha256"])
        with self.assertRaisesRegex(PASSPORT.PassportError, "lineage"):
            PASSPORT.export(self.passport_args, secret)

        self.passport_args.factory_sha = "b" * 40
        migrated = PASSPORT.migrate(self.passport_args, secret)
        self.assertEqual(migrated["factory_sha"], "b" * 40)
        self.assertEqual(len(migrated["migration_history"]), 1)

        self.state_args.factory_sha = "b" * 40
        self.state_args.role = "spec-linter"
        second = STATE.issue(self.state_args, "RUN spec-linter")
        self.assertRegex(second["passport_sha256"], r"^[0-9a-f]{64}$")
        self.state_args.receipt = second["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal(
            "run-2", "spec-linter", second["receipt_sha256"], "b" * 40
        )
        self.passport_args.receipt = second["receipt_sha256"]
        upgraded = PASSPORT.export(self.passport_args, secret)
        self.assertEqual(upgraded["cumulative_charges_micro_usd"], 3_000_000)
        self.assertEqual(len(upgraded["completed_role_evidence"]), 2)
        self.assertEqual(
            upgraded["migration_history"], migrated["migration_history"]
        )
        self.assertEqual(
            [item["factory_sha"] for item in upgraded["factory_release_history"]],
            ["a" * 40, "b" * 40],
        )

    def test_reverted_model_identity_success_is_restored_without_replay(
        self,
    ) -> None:
        route = {
            "kit_sha": "a" * 40,
            "resolution": {"selections": {"spec-linter": {
                "adapter": "cursor-anthropic",
                "reported_identity": "Opus 5 1M Medium Thinking",
                "route_id": "cursor-claude-opus-5-thinking-medium",
            }}},
            "schema": "ticket-model-route-plan/v1",
            "ticket": "T-110",
        }
        (self.product / "factory/route-plans/T-110.json").write_text(
            json.dumps(route, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        (self.product / "factory/tickets/T-110.md").write_text(
            f"# T-110\n\nState: Planning\n\nKit-SHA: {'a' * 40}\n\n"
            + "\n".join(f"Contract line {number}" for number in range(8))
            + "\n\n## Log\n",
            encoding="utf-8",
        )
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "pin old route", cwd=self.product)
        secret = PASSPORT.key(self.state_dir)
        planner = STATE.issue(self.state_args, "RUN planner")
        self.state_args.receipt = planner["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal("run-planner", "planner", planner["receipt_sha256"], "a" * 40)
        self.passport_args.receipt = planner["receipt_sha256"]
        PASSPORT.export(self.passport_args, secret)

        self.state_args.role = "spec-linter"
        lint = STATE.issue(self.state_args, "RUN spec-linter")
        self.state_args.receipt = lint["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        input_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            ticket.read_text(encoding="utf-8").replace(
                "## Log",
                "SPEC-LINT: PASS\n\n## Log",
            ),
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "spec lint output", cwd=self.product)
        output_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        run("git", "revert", "--no-edit", output_head, cwd=self.product)
        revert_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        run_id = "run-model-identity-success"
        self.model_identity_success_terminal(
            run_id, lint["receipt_sha256"], input_head
        )

        self.passport_args.factory_sha = "b" * 40
        self.passport_args.receipt = lint["receipt_sha256"]
        PASSPORT.migrate(self.passport_args, secret)
        current_selection = {
            "adapter": "cursor-anthropic",
            "reported_identity": "Opus 5 300K Medium",
            "route_id": "cursor-claude-opus-5-thinking-medium",
        }
        journal = {
            "kit_sha": "b" * 40,
            "revisions": [{"body": {
                "kind": "release-migration",
                "new_kit_sha": "b" * 40,
                "new_resolution": {
                    "selections": {"spec-linter": current_selection}
                },
            }}],
            "schema": "ticket-model-route-journal/v2",
            "ticket": "T-110",
        }
        (self.product / "factory/route-plans/T-110.json").write_text(
            json.dumps(journal, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        ticket.write_text(
            ticket.read_text(encoding="utf-8").replace(
                "a" * 40, "b" * 40
            ),
            encoding="utf-8",
        )
        run("git", "add", "factory/route-plans/T-110.json", cwd=self.product)
        run("git", "add", "factory/tickets/T-110.md", cwd=self.product)
        run("git", "commit", "-qm", "migrate route", cwd=self.product)
        PASSPORT.migrate(self.passport_args, secret)
        self.passport_args.factory_sha = "c" * 40
        PASSPORT.migrate(self.passport_args, secret)
        journal["kit_sha"] = "c" * 40
        journal["revisions"].append({"body": {
            "kind": "release-migration",
            "new_kit_sha": "c" * 40,
        }})
        (self.product / "factory/route-plans/T-110.json").write_text(
            json.dumps(journal, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        ticket.write_text(
            ticket.read_text(encoding="utf-8").replace(
                "b" * 40, "c" * 40
            ),
            encoding="utf-8",
        )
        run("git", "add", "factory/route-plans/T-110.json", cwd=self.product)
        run("git", "add", "factory/tickets/T-110.md", cwd=self.product)
        run("git", "commit", "-qm", "migrate route again", cwd=self.product)
        migration_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        PASSPORT.migrate(self.passport_args, secret)
        self.passport_args.run_id = run_id
        preflight = PASSPORT.verify_model_identity_success(
            self.passport_args, secret
        )
        self.assertEqual(preflight["recovery_status"], "restore-required")
        self.assertEqual(preflight["output_head"], output_head)
        self.assertEqual(preflight["revert_head"], revert_head)
        self.assertEqual(preflight["migration_head"], migration_head)
        self.assertEqual(preflight["migration_count"], 2)

        run("git", "revert", "--no-edit", revert_head, cwd=self.product)
        restored = PASSPORT.verify_model_identity_success(
            self.passport_args, secret
        )
        self.assertEqual(restored["recovery_status"], "restored")
        restored_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        ticket.write_text(
            ticket.read_text(encoding="utf-8") + "\nTAMPERED\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "tamper with restored output", cwd=self.product)
        with self.assertRaisesRegex(PASSPORT.PassportError, "topology"):
            PASSPORT.verify_model_identity_success(self.passport_args, secret)
        run("git", "reset", "--hard", restored_head, cwd=self.product)
        PASSPORT.migrate(self.passport_args, secret)
        failed = PASSPORT.export(self.passport_args, secret)
        self.assertFalse(any(
            item["run_id"] == run_id
            for item in failed["completed_role_evidence"]
        ))
        corrected = PASSPORT.correct_converged_success(
            self.passport_args, secret
        )
        matching = [
            item for item in corrected["completed_role_evidence"]
            if item["run_id"] == run_id
        ]
        self.assertEqual([item["role"] for item in matching], ["spec-linter"])
        self.assertEqual(
            corrected["completed_role_corrections"][-1]["issue"],
            PASSPORT.MODEL_IDENTITY_CORRECTION_ISSUE,
        )
        replayed = PASSPORT.correct_converged_success(
            self.passport_args, secret
        )
        self.assertEqual(replayed["passport_sha256"], corrected["passport_sha256"])

        (self.product / "unrelated").write_text("extra\n", encoding="utf-8")
        run("git", "add", "unrelated", cwd=self.product)
        run("git", "commit", "-qm", "unrelated descendant", cwd=self.product)
        with self.assertRaisesRegex(PASSPORT.PassportError, "topology"):
            PASSPORT.model_identity_recovery_topology(
                self.passport_args,
                PASSPORT.receipt(
                    self.state_dir, "T-110", lint["receipt_sha256"]
                ),
                PASSPORT.identity(self.passport_args),
            )

    def test_model_identity_recovery_rejects_conflicting_ticket_delta(self) -> None:
        ticket = self.product / "factory/tickets/T-110.md"
        base = run("git", "rev-parse", "HEAD", cwd=self.product)
        ticket.write_text("# T-110\n\nState: Current\n", encoding="utf-8")
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "current ticket change", cwd=self.product)
        current = run("git", "rev-parse", "HEAD", cwd=self.product)
        run("git", "reset", "--hard", base, cwd=self.product)
        ticket.write_text("# T-110\n\nState: Other\n", encoding="utf-8")
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "other ticket change", cwd=self.product)
        other = run("git", "rev-parse", "HEAD", cwd=self.product)
        with self.assertRaisesRegex(PASSPORT.PassportError, "topology"):
            PASSPORT.merged_ticket_blob(
                self.product, base, current, other,
                "factory/tickets/T-110.md",
            )

    def test_exact_converged_success_correction_is_authenticated_and_idempotent(
        self,
    ) -> None:
        secret = PASSPORT.key(self.state_dir)
        planner = STATE.issue(self.state_args, "RUN planner")
        self.state_args.receipt = planner["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal("run-planner", "planner", planner["receipt_sha256"], "a" * 40)
        self.passport_args.receipt = planner["receipt_sha256"]
        PASSPORT.export(self.passport_args, secret)

        self.state_args.role = "builder"
        builder = STATE.issue(self.state_args, "RUN builder")
        self.state_args.receipt = builder["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        passport_path = self.state_dir / "passports/T-110.json"
        input_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        (self.product / "builder-change").write_text("done\n", encoding="utf-8")
        run("git", "add", "builder-change", cwd=self.product)
        run("git", "commit", "-qm", "builder output", cwd=self.product)
        run_id = "run-converged-success"
        self.converged_success_terminal(
            run_id, builder["receipt_sha256"], input_head
        )
        self.passport_args.receipt = builder["receipt_sha256"]
        failed_output = PASSPORT.export(self.passport_args, secret)
        self.passport_args.action = "correct-converged-success"
        self.passport_args.factory_sha = "c" * 40
        self.passport_args.run_id = run_id
        with self.assertRaisesRegex(PASSPORT.PassportError, "authenticated lineage"):
            PASSPORT.correct_converged_success(self.passport_args, secret)
        self.assertEqual(
            failed_output["head_sha"],
            run("git", "rev-parse", "HEAD", cwd=self.product),
        )

        self.passport_args.factory_sha = "b" * 40
        PASSPORT.migrate(self.passport_args, secret)
        self.passport_args.factory_sha = "c" * 40
        twice_migrated = PASSPORT.migrate(self.passport_args, secret)
        self.assertEqual(len(twice_migrated["migration_history"]), 2)
        failed = twice_migrated
        self.assertFalse(any(
            item["run_id"] == run_id
            for item in failed["completed_role_evidence"]
        ))
        consumed = PASSPORT.receipt(
            self.state_dir, "T-110", builder["receipt_sha256"]
        )
        current = PASSPORT.identity(self.passport_args)
        self.assertFalse(PASSPORT.migrated_receipt_lineage(
            self.passport_args, failed, consumed, current
        ))
        self.assertTrue(PASSPORT.converged_success_migration_lineage(
            self.passport_args, failed, consumed, current
        ))
        wrong_parent = json.loads(json.dumps(failed))
        wrong_parent["migration_history"][-1][
            "from_passport_file_sha256"
        ] = "0" * 64
        self.assertFalse(PASSPORT.converged_success_migration_lineage(
            self.passport_args, wrong_parent, consumed, current
        ))
        wrong_route = json.loads(json.dumps(failed))
        wrong_route["migration_history"][0][
            "from_route_plan_sha256"
        ] = "0" * 64
        self.assertFalse(PASSPORT.converged_success_migration_lineage(
            self.passport_args, wrong_route, consumed, current
        ))

        ambiguous = {
            name: item for name, item in failed.items()
            if name not in {"authentication_sha256", "passport_sha256"}
        }
        identity_edge = dict(ambiguous["migration_history"][0])
        identity_edge["to_factory_sha"] = identity_edge["from_factory_sha"]
        identity_edge["to_head_sha"] = identity_edge["from_head_sha"]
        identity_edge["to_protected_base_sha"] = identity_edge[
            "from_protected_base_sha"
        ]
        identity_edge["to_route_plan_sha256"] = identity_edge[
            "from_route_plan_sha256"
        ]
        ambiguous["migration_history"] = [
            identity_edge, *ambiguous["migration_history"],
        ]
        PASSPORT.write_atomic(
            self.state_dir / "passports/T-110.json",
            PASSPORT.authenticate(ambiguous, secret),
        )
        with self.assertRaisesRegex(PASSPORT.PassportError, "authenticated lineage"):
            PASSPORT.correct_converged_success(self.passport_args, secret)
        PASSPORT.write_atomic(self.state_dir / "passports/T-110.json", failed)

        manifest = self.product / f"factory/runs/{run_id}.meta"
        terminal = manifest.read_bytes()
        failed_raw = passport_path.read_bytes()
        manifest.write_bytes(terminal.replace(
            b"adapter=cursor-openai", b"adapter=cursor-anthropic"
        ))
        anthropic = json.loads(failed_raw)
        anthropic.pop("authentication_sha256")
        anthropic.pop("passport_sha256")
        next(
            item for item in anthropic["charge_records"]
            if item["run_id"] == run_id
        )["manifest_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
        PASSPORT.write_atomic(
            passport_path, PASSPORT.authenticate(anthropic, secret)
        )
        anthropic_corrected = PASSPORT.correct_converged_success(
            self.passport_args, secret
        )
        self.assertEqual(
            sum(
                item["run_id"] == run_id
                for item in anthropic_corrected["completed_role_evidence"]
            ),
            1,
        )
        passport_path.write_bytes(failed_raw)
        os.chmod(passport_path, 0o600)
        manifest.write_bytes(terminal)

        manifest.write_bytes(terminal.replace(
            b"adapter=cursor-openai", b"adapter=codex"
        ))
        with self.assertRaisesRegex(PASSPORT.PassportError, "typed converged"):
            PASSPORT.correct_converged_success(self.passport_args, secret)
        manifest.write_bytes(terminal)

        corrected = PASSPORT.correct_converged_success(
            self.passport_args, secret
        )
        matching = [
            item for item in corrected["completed_role_evidence"]
            if item["run_id"] == run_id
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(
            corrected["completed_role_corrections"],
            [{
                "failed_factory_sha": "a" * 40,
                "issue": PASSPORT.COMPLETION_CORRECTION_ISSUE,
                "output_head_sha": run(
                    "git", "rev-parse", "HEAD", cwd=self.product
                ),
                "progress_events": 2,
                "progress_journal_sha256": hashlib.sha256(
                    (
                        self.product
                        / f"factory/runs/{run_id}.progress.jsonl"
                    ).read_bytes()
                ).hexdigest(),
                "recovery_factory_sha": "c" * 40,
                "receipt_parent_file_sha256": builder["passport_sha256"],
                "run_id": run_id,
                "schema": PASSPORT.COMPLETION_CORRECTION_SCHEMA,
                "transition_receipt_sha256": builder["receipt_sha256"],
            }],
        )
        replayed = PASSPORT.correct_converged_success(
            self.passport_args, secret
        )
        self.assertEqual(
            replayed["passport_sha256"], corrected["passport_sha256"]
        )

        self.passport_args.run_id = "wrong-run"
        with self.assertRaisesRegex(PASSPORT.PassportError, "authenticated lineage"):
            PASSPORT.correct_converged_success(self.passport_args, secret)
        self.passport_args.run_id = run_id
        progress = self.product / f"factory/runs/{run_id}.progress.jsonl"
        original_progress = progress.read_bytes()
        progress.write_bytes(original_progress.replace(
            b'"subtype":"success","type":"result"',
            b'"subtype":"completed","type":"tool_call"',
        ))
        os.chmod(progress, 0o600)
        with self.assertRaisesRegex(PASSPORT.PassportError, "terminal success"):
            PASSPORT.correct_converged_success(self.passport_args, secret)
        progress.write_bytes(original_progress)
        os.chmod(progress, 0o600)

        output = self.product / f"factory/runs/{run_id}.out"
        original_output = output.read_bytes()
        output.write_bytes(original_output + b"tamper\n")
        os.chmod(output, 0o600)
        with self.assertRaisesRegex(PASSPORT.PassportError, "typed converged"):
            PASSPORT.correct_converged_success(self.passport_args, secret)
        output.write_bytes(original_output)
        os.chmod(output, 0o600)

        dirty = self.product / "untracked"
        dirty.write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(PASSPORT.PassportError, "clean execution cell"):
            PASSPORT.correct_converged_success(self.passport_args, secret)
        dirty.unlink()

        authenticated = passport_path.read_bytes()
        wrong_charge = json.loads(authenticated)
        wrong_charge.pop("authentication_sha256")
        wrong_charge.pop("passport_sha256")
        next(
            item for item in wrong_charge["charge_records"]
            if item["run_id"] == run_id
        )["manifest_sha256"] = "f" * 64
        PASSPORT.write_atomic(
            passport_path, PASSPORT.authenticate(wrong_charge, secret)
        )
        with self.assertRaisesRegex(PASSPORT.PassportError, "run charge is missing"):
            PASSPORT.correct_converged_success(self.passport_args, secret)
        passport_path.write_bytes(authenticated)
        os.chmod(passport_path, 0o600)

        passport_path.write_bytes(authenticated.replace(b'"builder"', b'"tamper!"', 1))
        os.chmod(passport_path, 0o600)
        with self.assertRaisesRegex(PASSPORT.PassportError, "digest is invalid"):
            PASSPORT.correct_converged_success(self.passport_args, secret)
        passport_path.write_bytes(authenticated)
        os.chmod(passport_path, 0o600)

        self.state_args.factory_sha = "c" * 40
        self.state_args.role = "reviewer"
        reviewer = STATE.issue(self.state_args, "RUN reviewer")
        self.state_args.receipt = reviewer["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal(
            "run-reviewer", "reviewer", reviewer["receipt_sha256"], "c" * 40
        )
        self.passport_args.action = "export"
        self.passport_args.receipt = reviewer["receipt_sha256"]
        next_export = PASSPORT.export(self.passport_args, secret)
        self.assertEqual(
            next_export["completed_role_corrections"],
            corrected["completed_role_corrections"],
        )

    def test_terminal_export_accepts_exact_authenticated_release_migration(self) -> None:
        secret = PASSPORT.key(self.state_dir)
        first = STATE.issue(self.state_args, "RUN planner")
        self.state_args.receipt = first["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal("run-1", "planner", first["receipt_sha256"], "a" * 40)
        self.passport_args.receipt = first["receipt_sha256"]
        PASSPORT.export(self.passport_args, secret)

        self.state_args.role = "spec-linter"
        second = STATE.issue(self.state_args, "RUN spec-linter")
        self.state_args.receipt = second["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal(
            "run-2", "spec-linter", second["receipt_sha256"], "a" * 40
        )

        route = self.product / "factory/route-plans/T-110.json"
        route.write_text(
            f'{{"kit_sha":"{"b" * 40}","ticket":"T-110"}}\n',
            encoding="utf-8",
        )
        run("git", "add", str(route), cwd=self.product)
        run("git", "commit", "-qm", "migrate release", cwd=self.product)
        self.passport_args.factory_sha = "b" * 40
        migrated = PASSPORT.migrate(self.passport_args, secret)
        self.assertEqual(
            migrated["parent_file_sha256"], second["passport_sha256"]
        )

        self.passport_args.receipt = second["receipt_sha256"]
        passport = self.state_dir / "passports/T-110.json"
        before = passport.read_bytes()
        clean_route = route.read_text(encoding="utf-8")
        route.write_text(clean_route + "dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(PASSPORT.PassportError, "clean execution cell"):
            PASSPORT.export(self.passport_args, secret)
        self.assertEqual(passport.read_bytes(), before)
        route.write_text(clean_route, encoding="utf-8")
        exported = PASSPORT.export(self.passport_args, secret)
        self.assertEqual(
            [item["role"] for item in exported["completed_role_evidence"]],
            ["planner", "spec-linter"],
        )
        self.assertEqual(exported["cumulative_charges_micro_usd"], 3_000_000)

    def test_terminal_export_accepts_only_a_contiguous_migration_suffix(self) -> None:
        secret = PASSPORT.key(self.state_dir)
        first = STATE.issue(self.state_args, "RUN planner")
        self.state_args.receipt = first["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal("run-1", "planner", first["receipt_sha256"], "a" * 40)
        self.passport_args.receipt = first["receipt_sha256"]
        PASSPORT.export(self.passport_args, secret)

        self.state_args.role = "spec-linter"
        second = STATE.issue(self.state_args, "RUN spec-linter")
        self.state_args.receipt = second["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal(
            "run-2", "spec-linter", second["receipt_sha256"], "a" * 40
        )

        route = self.product / "factory/route-plans/T-110.json"
        for factory_sha in ("b" * 40, "c" * 40):
            route.write_text(
                json.dumps({"kit_sha": factory_sha, "ticket": "T-110"}) + "\n",
                encoding="utf-8",
            )
            run("git", "add", str(route), cwd=self.product)
            run("git", "commit", "-qm", f"migrate to {factory_sha[0]}", cwd=self.product)
            self.passport_args.factory_sha = factory_sha
            migrated = PASSPORT.migrate(self.passport_args, secret)

        self.passport_args.receipt = second["receipt_sha256"]
        passport = self.state_dir / "passports/T-110.json"
        manifest = self.product / "factory/runs/run-2.meta"
        terminal = manifest.read_text(encoding="utf-8")
        manifest.write_text(
            terminal.replace(f"kit_sha={'a' * 40}", f"kit_sha={'f' * 40}"),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            PASSPORT.PassportError, "terminal role evidence is missing"
        ):
            PASSPORT.export(self.passport_args, secret)
        manifest.write_text(terminal, encoding="utf-8")

        protected = self.root / "protected-base-advance"
        run("git", "clone", "-q", str(self.remote), str(protected), cwd=self.root)
        run("git", "config", "user.name", "Test", cwd=protected)
        run("git", "config", "user.email", "test@example.invalid", cwd=protected)
        marker = protected / "factory/base-advance"
        marker.write_text("new protected base\n", encoding="utf-8")
        run("git", "add", str(marker), cwd=protected)
        run("git", "commit", "-qm", "advance protected base", cwd=protected)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=protected)
        run("git", "fetch", "-q", "origin", "main", cwd=self.product)
        with self.assertRaisesRegex(PASSPORT.PassportError, "lineage"):
            PASSPORT.export(self.passport_args, secret)
        migrated = PASSPORT.migrate(self.passport_args, secret)

        unsigned = {
            name: item for name, item in migrated.items()
            if name not in {"authentication_sha256", "passport_sha256"}
        }
        unsigned["migration_history"] = [
            dict(item) for item in unsigned["migration_history"]
        ]
        unsigned["migration_history"][0]["from_passport_file_sha256"] = "f" * 64
        PASSPORT.write_atomic(passport, PASSPORT.authenticate(unsigned, secret))
        with self.assertRaisesRegex(PASSPORT.PassportError, "lineage"):
            PASSPORT.export(self.passport_args, secret)

        PASSPORT.write_atomic(passport, migrated)
        exported = PASSPORT.export(self.passport_args, secret)
        self.assertEqual(
            [item["role"] for item in exported["completed_role_evidence"]],
            ["planner", "spec-linter"],
        )
        self.assertEqual(exported["cumulative_charges_micro_usd"], 3_000_000)
        self.assertEqual(len(exported["charge_records"]), 2)

    def test_protected_authorization_bridges_one_exact_legacy_snapshot(self) -> None:
        secret = PASSPORT.key(self.state_dir)
        first = STATE.issue(self.state_args, "RUN planner")
        self.state_args.receipt = first["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal("run-1", "planner", first["receipt_sha256"], "a" * 40)
        self.passport_args.receipt = first["receipt_sha256"]
        PASSPORT.export(self.passport_args, secret)

        self.state_args.role = "spec-linter"
        second = STATE.issue(self.state_args, "RUN spec-linter")
        self.state_args.receipt = second["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal(
            "run-2", "spec-linter", second["receipt_sha256"], "a" * 40
        )
        terminal = self.product / "factory/runs/run-2.meta"
        terminal.write_text(
            terminal.read_text(encoding="utf-8").replace(
                "accounting_state=completed\n",
                "accounting_state=abandoned_conservative\n"
                "reserved_usd=1.500000\n"
                "cost_basis=conservative_reservation\n",
            ),
            encoding="utf-8",
        )

        route = self.product / "factory/route-plans/T-110.json"
        for factory_sha in ("b" * 40, "c" * 40):
            route.write_text(
                json.dumps({"kit_sha": factory_sha, "ticket": "T-110"}) + "\n",
                encoding="utf-8",
            )
            run("git", "add", str(route), cwd=self.product)
            run("git", "commit", "-qm", f"legacy migrate to {factory_sha[0]}", cwd=self.product)
            self.passport_args.factory_sha = factory_sha
            migrated = PASSPORT.migrate(self.passport_args, secret)

        unsigned = {
            name: item for name, item in migrated.items()
            if name not in {"authentication_sha256", "passport_sha256"}
        }
        legacy_fields = {
            "from_factory_sha", "from_head_sha", "from_protected_base_sha",
            "to_factory_sha", "to_head_sha", "to_protected_base_sha",
        }
        unsigned["migration_history"] = [
            {name: item[name] for name in legacy_fields}
            for item in unsigned["migration_history"]
        ]
        legacy = PASSPORT.authenticate(unsigned, secret)
        passport = self.state_dir / "passports/T-110.json"
        PASSPORT.write_atomic(passport, legacy)

        route.write_text(
            json.dumps({"kit_sha": "d" * 40, "ticket": "T-110"}) + "\n",
            encoding="utf-8",
        )
        run("git", "add", str(route), cwd=self.product)
        run("git", "commit", "-qm", "target legacy bridge", cwd=self.product)
        self.passport_args.factory_sha = "d" * 40
        self.passport_args.receipt = second["receipt_sha256"]
        authorization = PASSPORT.authorize_lineage(self.passport_args, secret)
        self.assertEqual(
            authorization["terminal"]["accounting_state"],
            "abandoned_conservative",
        )

        protected = self.root / "protected-lineage"
        run("git", "clone", "-q", str(self.remote), str(protected), cwd=self.root)
        run("git", "config", "user.name", "Test", cwd=protected)
        run("git", "config", "user.email", "test@example.invalid", cwd=protected)
        relative = PASSPORT.lineage_authorization_path("d" * 40, "T-110")
        path = protected / relative
        path.parent.mkdir(parents=True)
        path.write_bytes(PASSPORT.canonical(authorization))
        application = protected / "factory/PROJECT.env"
        application.write_text(
            application.read_text(encoding="utf-8") + "UNRELATED_DRIFT=1\n",
            encoding="utf-8",
        )
        run("git", "add", relative, str(application), cwd=protected)
        run("git", "commit", "-qm", "mix authorization with application", cwd=protected)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=protected)
        run("git", "fetch", "-q", "origin", "main", cwd=self.product)
        with self.assertRaisesRegex(
            PASSPORT.PassportError, "lineage authorization is invalid"
        ):
            PASSPORT.migrate(self.passport_args, secret)

        route.write_text(
            json.dumps({"kit_sha": "e" * 40, "ticket": "T-110"}) + "\n",
            encoding="utf-8",
        )
        run("git", "add", str(route), cwd=self.product)
        run("git", "commit", "-qm", "retarget exact legacy bridge", cwd=self.product)
        self.passport_args.factory_sha = "e" * 40
        authorization = PASSPORT.authorize_lineage(self.passport_args, secret)
        relative = PASSPORT.lineage_authorization_path("e" * 40, "T-110")
        path = protected / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PASSPORT.canonical(authorization))
        run("git", "add", relative, cwd=protected)
        run("git", "commit", "-qm", "authorize exact legacy bridge", cwd=protected)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=protected)
        run("git", "fetch", "-q", "origin", "main", cwd=self.product)
        bridged = PASSPORT.migrate(self.passport_args, secret)
        edge = bridged["migration_history"][-1]
        self.assertRegex(
            edge["lineage_authorization_sha256"], r"^[0-9a-f]{64}$"
        )
        tampered = {
            name: item for name, item in bridged.items()
            if name not in {"authentication_sha256", "passport_sha256"}
        }
        tampered["migration_history"] = [
            dict(item) for item in tampered["migration_history"]
        ]
        tampered["migration_history"][-1][
            "lineage_authorization_sha256"
        ] = "e" * 64
        PASSPORT.write_atomic(
            passport, PASSPORT.authenticate(tampered, secret)
        )
        with self.assertRaisesRegex(PASSPORT.PassportError, "lineage"):
            PASSPORT.export(self.passport_args, secret)
        PASSPORT.write_atomic(passport, bridged)
        exported = PASSPORT.export(self.passport_args, secret)
        self.assertEqual(
            [item["role"] for item in exported["completed_role_evidence"]],
            ["planner", "spec-linter"],
        )
        self.assertEqual(exported["cumulative_charges_micro_usd"], 3_000_000)
        with self.assertRaisesRegex(PASSPORT.PassportError, "lineage"):
            PASSPORT.export(self.passport_args, secret)

    def test_protected_inflight_authorization_allows_exact_rewrite(self) -> None:
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            ticket.read_text(encoding="utf-8").replace(
                "State: Planning", "State: Blocked-Escalated"
            ) + "\nResume-State: Planning\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "materialize blocked ticket", cwd=self.product)
        secret = PASSPORT.key(self.state_dir)
        receipt = STATE.issue(self.state_args, "RUN planner")
        self.state_args.receipt = receipt["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal("run-1", "planner", receipt["receipt_sha256"], "a" * 40)
        self.passport_args.receipt = receipt["receipt_sha256"]
        previous = PASSPORT.export(self.passport_args, secret)

        rewritten = run(
            "git", "commit-tree", "HEAD^{tree}", "-m", "authorized rewrite",
            cwd=self.product,
        )
        run("git", "reset", "--hard", rewritten, cwd=self.product)
        with self.assertRaisesRegex(PASSPORT.PassportError, "lineage"):
            PASSPORT.migrate(self.passport_args, secret)
        self.passport_args.factory_sha = "b" * 40
        with self.assertRaisesRegex(PASSPORT.PassportError, "lineage"):
            PASSPORT.migrate(self.passport_args, secret)

        protected = self.root / "protected"
        run("git", "clone", "-q", str(self.remote), str(protected), cwd=self.root)
        run("git", "config", "user.name", "Test", cwd=protected)
        run("git", "config", "user.email", "test@example.invalid", cwd=protected)
        authorization = protected / (
            "factory/migrations/inflight-release/" + "b" * 40 + ".json"
        )
        authorization.parent.mkdir(parents=True)
        authorization.write_text(
            json.dumps({
                "repository": "nysa-company/relay-factory",
                "schema": PASSPORT.INFLIGHT_SCHEMA,
                "source_kit_sha": "a" * 40,
                "target_kit_sha": "b" * 40,
                "tickets": [{
                    "branch": "ticket/T-110",
                    "head": rewritten,
                    "state": "Blocked-Escalated",
                    "ticket": "T-110",
                }],
            }, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        run("git", "add", ".", cwd=protected)
        run("git", "commit", "-qm", "authorize rewrite", cwd=protected)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=protected)
        run("git", "fetch", "-q", "origin", "main", cwd=self.product)

        migrated = PASSPORT.migrate(self.passport_args, secret)
        self.assertEqual(migrated["head_sha"], rewritten)
        self.assertEqual(migrated["parent_digest"], previous["passport_sha256"])
        self.assertEqual(migrated["factory_sha"], "b" * 40)
        self.assertEqual(migrated["current_state"], "Blocked-Escalated")

    def test_protected_same_release_test_rewrite_is_exact_and_charged(self) -> None:
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text("# T-110\n\nState: Building\n", encoding="utf-8")
        test = self.product / "app/tests/detail.test.js"
        test.parent.mkdir(parents=True)
        test.write_text("old test\n", encoding="utf-8")
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "enter building", cwd=self.product)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=self.product)

        secret = PASSPORT.key(self.state_dir)
        prior_receipt = STATE.issue(self.state_args, "RUN planner")
        self.state_args.receipt = prior_receipt["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal(
            "run-prior", "planner", prior_receipt["receipt_sha256"], "a" * 40
        )
        self.passport_args.receipt = prior_receipt["receipt_sha256"]
        previous = PASSPORT.export(self.passport_args, secret)
        old_head = previous["head_sha"]

        self.state_args.role = "test-author"
        repair = STATE.issue(self.state_args, "FIX test-author")
        self.state_args.receipt = repair["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        output = self.product / "factory/runs/run-repair.out"
        output.write_text("repair output\n", encoding="utf-8")
        os.chmod(output, 0o600)
        output_digest = hashlib.sha256(output.read_bytes()).hexdigest()
        (self.product / "factory/runs/run-repair.meta").write_text(
            "run_id=run-repair\n"
            "phase=completed\n"
            "accounting_state=abandoned_conservative\n"
            "task_submitted=1\n"
            "effective_cost=2.000000\n"
            "exit_status=11\n"
            "ticket=T-110\n"
            "role=test-author\n"
            "role_exit=role_exit_push_failed\n"
            f"role_head_before={old_head}\n"
            f"kit_sha={'a' * 40}\n"
            "contract_version=1.8.0\n"
            f"transition_receipt_sha256={repair['receipt_sha256']}\n"
            f"output_sha256={output_digest}\n",
            encoding="utf-8",
        )

        test.write_text("repaired test\n", encoding="utf-8")
        ticket.write_text(
            "# T-110\n\nState: Building\n\nTest-author repair recorded.\n",
            encoding="utf-8",
        )
        run("git", "add", ".", cwd=self.product)
        tree = run("git", "write-tree", cwd=self.product)
        rewritten = run(
            "git", "commit-tree", tree, "-m", "test repair rewrite",
            cwd=self.product,
        )
        run("git", "reset", "--hard", rewritten, cwd=self.product)
        with self.assertRaisesRegex(PASSPORT.PassportError, "lineage"):
            PASSPORT.migrate(self.passport_args, secret)

        (self.product / "app/server.js").write_text(
            "unknown semantic change\n", encoding="utf-8"
        )
        run("git", "add", ".", cwd=self.product)
        unsafe_tree = run("git", "write-tree", cwd=self.product)
        unsafe = run(
            "git", "commit-tree", unsafe_tree, "-m", "unsafe rewrite",
            cwd=self.product,
        )
        self.assertFalse(PASSPORT.rewrite_delta_allowed(
            self.product, old_head, unsafe, "app/tests/", "T-110"
        ))
        run("git", "reset", "--hard", rewritten, cwd=self.product)

        protected = self.root / "protected-rewrite"
        run("git", "clone", "-q", str(self.remote), str(protected), cwd=self.root)
        run("git", "config", "user.name", "Test", cwd=protected)
        run("git", "config", "user.email", "test@example.invalid", cwd=protected)
        authorization = (
            protected / f"factory/migrations/ticket-rewrite/{rewritten}.json"
        )
        authorization.parent.mkdir(parents=True)
        authorization.write_text(
            json.dumps({
                "branch": "ticket/T-110",
                "factory_sha": "a" * 40,
                "head": rewritten,
                "passport_sha256": previous["passport_sha256"],
                "previous_head": old_head,
                "repository": "nysa-company/relay-factory",
                "role": "test-author",
                "route_plan_sha256": hashlib.sha256(
                    (self.product / "factory/route-plans/T-110.json").read_bytes()
                ).hexdigest(),
                "schema": PASSPORT.REWRITE_SCHEMA,
                "state": "Building",
                "ticket": "T-110",
                "transition_receipt_sha256": repair["receipt_sha256"],
            }, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        run("git", "add", ".", cwd=protected)
        run("git", "commit", "-qm", "authorize exact test rewrite", cwd=protected)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=protected)
        run("git", "fetch", "-q", "origin", "main", cwd=self.product)

        migrated = PASSPORT.migrate(self.passport_args, secret)
        self.assertEqual(migrated["factory_sha"], "a" * 40)
        self.assertEqual(migrated["head_sha"], rewritten)
        self.assertEqual(migrated["cumulative_charges_micro_usd"], 3_500_000)
        self.assertEqual(len(migrated["completed_role_evidence"]), 1)
        self.assertRegex(
            migrated["migration_history"][-1]["rewrite_authorization_sha256"],
            r"^[0-9a-f]{64}$",
        )

    def test_failed_mixed_history_repair_survives_successor_and_base_advance(self) -> None:
        replay_base = run("git", "rev-parse", "HEAD", cwd=self.product)
        ticket = self.product / "factory/tickets/T-110.md"
        old_ticket = "# T-110\n\nState: Building\n\nFrozen contract is unchanged.\n"
        ticket.write_text(old_ticket, encoding="utf-8")
        test = self.product / "app/tests/detail.test.js"
        test.parent.mkdir(parents=True)
        test.write_text("acceptance test\n", encoding="utf-8")
        checker = self.product / "app/check.js"
        checker.write_text("checker\n", encoding="utf-8")
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "mixed test and checker", cwd=self.product)
        implementation = self.product / "app/server.js"
        implementation.write_text("implementation\n", encoding="utf-8")
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "implementation", cwd=self.product)
        old_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        old_tree = run("git", "rev-parse", "HEAD^{tree}", cwd=self.product)
        run(
            "git", "push", "-q", "origin",
            f"{old_head}:refs/heads/ticket/T-110", cwd=self.product,
        )

        secret = PASSPORT.key(self.state_dir)
        prior = STATE.issue(self.state_args, "RUN planner")
        self.state_args.receipt = prior["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal("run-prior", "planner", prior["receipt_sha256"], "a" * 40)
        self.passport_args.receipt = prior["receipt_sha256"]
        PASSPORT.export(self.passport_args, secret)

        protected = self.root / "protected-history-repair"
        run("git", "clone", "-q", str(self.remote), str(protected), cwd=self.root)
        run("git", "config", "user.name", "Test", cwd=protected)
        run("git", "config", "user.email", "test@example.invalid", cwd=protected)
        (protected / "protected.txt").write_text("advanced\n", encoding="utf-8")
        run("git", "add", ".", cwd=protected)
        run("git", "commit", "-qm", "advance protected main", cwd=protected)
        authorization_parent = run("git", "rev-parse", "HEAD", cwd=protected)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=protected)
        run("git", "fetch", "-q", "origin", "main", cwd=self.product)
        previous = PASSPORT.migrate(self.passport_args, secret)
        self.assertEqual(previous["protected_base_sha"], authorization_parent)
        self.assertIn(replay_base, previous["base_history"])

        self.state_args.role = "test-author"
        repair = STATE.issue(self.state_args, "RUN test-author")
        self.state_args.receipt = repair["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        output = self.product / "factory/runs/run-history-repair.out"
        output.write_text("history repair complete\n", encoding="utf-8")
        os.chmod(output, 0o600)
        output_digest = hashlib.sha256(output.read_bytes()).hexdigest()
        (self.product / "factory/runs/run-history-repair.meta").write_text(
            "run_id=run-history-repair\n"
            "phase=completed\n"
            "accounting_state=abandoned_conservative\n"
            "task_submitted=1\n"
            "effective_cost=2.000000\n"
            "exit_status=11\n"
            "ticket=T-110\n"
            "role=test-author\n"
            "role_exit=role_exit_push_failed\n"
            f"role_head_before={old_head}\n"
            f"kit_sha={'a' * 40}\n"
            "contract_version=1.8.0\n"
            f"transition_receipt_sha256={repair['receipt_sha256']}\n"
            f"output_sha256={output_digest}\n",
            encoding="utf-8",
        )

        self.passport_args.factory_sha = "b" * 40
        previous = PASSPORT.migrate(self.passport_args, secret)
        self.assertEqual(previous["factory_sha"], "b" * 40)
        self.assertEqual(previous["factory_release_history"], [
            {"contract_version": "1.8.0", "factory_sha": "a" * 40},
            {"contract_version": "1.8.0", "factory_sha": "b" * 40},
        ])

        run("git", "reset", "--hard", replay_base, cwd=self.product)
        ticket.write_text(old_ticket, encoding="utf-8")
        run("git", "add", str(ticket.relative_to(self.product)), cwd=self.product)
        run("git", "commit", "-qm", "ticket state", cwd=self.product)
        test.parent.mkdir(parents=True)
        test.write_text("acceptance test\n", encoding="utf-8")
        run("git", "add", str(test.relative_to(self.product)), cwd=self.product)
        run("git", "commit", "-qm", "test only", cwd=self.product)
        checker.write_text("checker\n", encoding="utf-8")
        run("git", "add", str(checker.relative_to(self.product)), cwd=self.product)
        run("git", "commit", "-qm", "checker only", cwd=self.product)
        implementation.write_text("implementation\n", encoding="utf-8")
        run(
            "git", "add", str(implementation.relative_to(self.product)),
            cwd=self.product,
        )
        run("git", "commit", "-qm", "implementation", cwd=self.product)
        self.assertEqual(
            run("git", "rev-parse", "HEAD^{tree}", cwd=self.product), old_tree
        )
        ticket.write_text(
            old_ticket + "\nTest-author history repair recorded.\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket.relative_to(self.product)), cwd=self.product)
        run("git", "commit", "-qm", "record history repair", cwd=self.product)
        rewritten = run("git", "rev-parse", "HEAD", cwd=self.product)
        current = PASSPORT.identity(self.passport_args)
        route = previous["route_plan_sha256"]
        issued = int(PASSPORT.time.time()) - 1
        authorization = {
            "authorization_parent": authorization_parent,
            "branch": "ticket/T-110",
            "expires_at_epoch": issued + 3600,
            "factory_sha": "b" * 40,
            "failed_test_factory_sha": "a" * 40,
            "failed_test_receipt_sha256": repair["receipt_sha256"],
            "failed_test_run_id": "run-history-repair",
            "force_with_lease_head": old_head,
            "head": rewritten,
            "head_tree": current["head_tree"],
            "issue": "https://github.com/nysa-company/software-factory/issues/348",
            "issued_at_epoch": issued,
            "mode": "failed-push-history-repair",
            "operator": "test-operator",
            "passport_sha256": previous["passport_sha256"],
            "previous_head": old_head,
            "previous_tree": old_tree,
            "replay_base": replay_base,
            "repository": "nysa-company/relay-factory",
            "route_plan_sha256": route,
            "schema": PASSPORT.HISTORY_REPAIR_SCHEMA,
            "state": "Building",
            "ticket": "T-110",
        }
        expired = dict(authorization, expires_at_epoch=issued)
        self.assertIsNone(PASSPORT.authorized_history_repair(
            self.passport_args, previous, current, "Building",
            authorization_parent, expired,
            json.dumps(expired, sort_keys=True, separators=(",", ":")),
            f"factory/migrations/ticket-rewrite/{rewritten}.json",
            "nysa-company/relay-factory", "app/tests/", route,
        ))
        mismatched_failed_factory = dict(
            authorization, failed_test_factory_sha="b" * 40
        )
        self.assertIsNone(PASSPORT.authorized_history_repair(
            self.passport_args, previous, current, "Building",
            authorization_parent, mismatched_failed_factory,
            json.dumps(
                mismatched_failed_factory,
                sort_keys=True,
                separators=(",", ":"),
            ),
            f"factory/migrations/ticket-rewrite/{rewritten}.json",
            "nysa-company/relay-factory", "app/tests/", route,
        ))
        unknown_failed_factory = dict(
            authorization, failed_test_factory_sha="c" * 40
        )
        self.assertIsNone(PASSPORT.authorized_history_repair(
            self.passport_args, previous, current, "Building",
            authorization_parent, unknown_failed_factory,
            json.dumps(
                unknown_failed_factory, sort_keys=True, separators=(",", ":")
            ),
            f"factory/migrations/ticket-rewrite/{rewritten}.json",
            "nysa-company/relay-factory", "app/tests/", route,
        ))
        self.assertTrue(PASSPORT.failed_rewrite_manifest(
            self.passport_args, previous, repair["receipt_sha256"], "a" * 40
        ))
        self.assertFalse(PASSPORT.failed_rewrite_manifest(
            self.passport_args, previous, repair["receipt_sha256"], "b" * 40
        ))
        self.assertFalse(PASSPORT.verified_history_repair(
            str(self.product), authorization_parent, old_head, rewritten,
            ["app/tests/"],
            "factory/ conformance/factory/ .gitignore context/memory.md".split(),
        ))

        path = protected / f"factory/migrations/ticket-rewrite/{rewritten}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(authorization, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        run("git", "add", ".", cwd=protected)
        run("git", "commit", "-qm", "authorize exact history repair", cwd=protected)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=protected)
        run(
            "git", "push", "-q",
            f"--force-with-lease=refs/heads/ticket/T-110:{old_head}",
            "origin", f"{rewritten}:refs/heads/ticket/T-110", cwd=self.product,
        )
        run("git", "fetch", "-q", "origin", "main", cwd=self.product)

        migrated = PASSPORT.migrate(self.passport_args, secret)
        self.assertEqual(migrated["head_sha"], rewritten)
        self.assertEqual(
            migrated["migration_history"][-1]["from_protected_base_sha"],
            authorization_parent,
        )
        self.assertRegex(
            migrated["migration_history"][-1]["rewrite_authorization_sha256"],
            r"^[0-9a-f]{64}$",
        )
        failed_charges = [
            item for item in migrated["charge_records"]
            if item["run_id"] == "run-history-repair"
        ]
        self.assertEqual(len(failed_charges), 1)
        self.assertFalse(any(
            item["run_id"] == "run-history-repair"
            for item in migrated["completed_role_evidence"]
        ))
        self.passport_args.receipt = repair["receipt_sha256"]
        exported = PASSPORT.export(self.passport_args, secret)
        self.assertEqual(exported["head_sha"], rewritten)
        self.assertEqual(PASSPORT.migrate(self.passport_args, secret), exported)

    def test_accepted_late_test_merge_history_normalizes_with_exact_evidence(self) -> None:
        protected = self.root / "protected-normalization"
        run("git", "clone", "-q", str(self.remote), str(protected), cwd=self.root)
        run("git", "config", "user.name", "Test", cwd=protected)
        run("git", "config", "user.email", "test@example.invalid", cwd=protected)
        (protected / "protected.txt").write_text("protected base\n", encoding="utf-8")
        run("git", "add", ".", cwd=protected)
        run("git", "commit", "-qm", "protected base advance", cwd=protected)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=protected)
        base = run("git", "rev-parse", "HEAD", cwd=protected)
        run("git", "fetch", "-q", "origin", "main", cwd=self.product)

        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            "# T-110\n\nState: Blocked-Escalated\nResume-State: Building\n\n"
            "## Frozen contract — version 6\n"
            "- **Freeze result — PASS.** Contract version 6 is frozen.\n",
            encoding="utf-8",
        )
        test = self.product / "app/tests/detail.test.js"
        test.parent.mkdir(parents=True)
        test.write_text("v6 initial\n", encoding="utf-8")
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "test-author v6 initial", cwd=self.product)
        implementation = self.product / "app/server.js"
        implementation.write_text("v6 implementation\n", encoding="utf-8")
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "builder v6", cwd=self.product)

        self.state_args.role = "test-author"
        repair = STATE.issue(self.state_args, "FIX test-author")
        self.state_args.receipt = repair["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal(
            "accepted-late-test", "test-author", repair["receipt_sha256"],
            "a" * 40,
        )
        test.write_text("v6 initial\nv6 accepted late repair\n", encoding="utf-8")
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "test-author accepted late v6 repair", cwd=self.product)
        run(
            "git", "merge", "-q", "--no-ff", "origin/main", "-m",
            "merge protected base", cwd=self.product,
        )
        ticket.write_text(
            ticket.read_text(encoding="utf-8")
            + "\n## Frozen contract — version 7\n"
            + "- **Freeze result — PASS.** Contract version 7 is frozen.\n",
            encoding="utf-8",
        )
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "planner v7", cwd=self.product)
        test.write_text(test.read_text(encoding="utf-8") + "v7\n", encoding="utf-8")
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "test-author v7", cwd=self.product)
        implementation.write_text("v6 implementation\nv7 implementation\n", encoding="utf-8")
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "builder v7", cwd=self.product)
        ticket.write_text(
            ticket.read_text(encoding="utf-8")
            + "\n## Frozen contract — version 8\n"
            + "- **Freeze result — PASS.** Contract version 8 is frozen.\n",
            encoding="utf-8",
        )
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "planner v8", cwd=self.product)

        secret = PASSPORT.key(self.state_dir)
        self.passport_args.receipt = repair["receipt_sha256"]
        predecessor = PASSPORT.export(self.passport_args, secret)
        old_head = predecessor["head_sha"]
        run(
            "git", "push", "-q", "origin",
            f"{old_head}:refs/heads/ticket/T-110", cwd=self.product,
        )
        self.passport_args.factory_sha = "b" * 40
        previous = PASSPORT.migrate(self.passport_args, secret)
        self.assertEqual(previous["factory_sha"], "b" * 40)
        self.assertEqual(previous["head_sha"], old_head)
        self.assertIn(
            {"contract_version": "1.8.0", "factory_sha": "a" * 40},
            previous["factory_release_history"],
        )
        old_evidence = previous["completed_role_evidence"]
        old_charges = previous["charge_records"]
        subprocess.run(
            [
                "bash", str(ROOT / "scripts/reorder-test-fixes.sh"),
                "--base", base, "--test-paths", "app/tests/",
                "--exempt-paths", "factory/",
            ],
            cwd=self.product, text=True, capture_output=True, check=True,
        )
        rewritten = run("git", "rev-parse", "HEAD", cwd=self.product)
        self.assertNotEqual(rewritten, old_head)
        self.assertEqual(run("git", "rev-parse", "HEAD^{tree}", cwd=self.product), previous["head_tree"])
        with self.assertRaisesRegex(PASSPORT.PassportError, "lineage"):
            PASSPORT.migrate(self.passport_args, secret)

        authorization = (
            protected / f"factory/migrations/ticket-rewrite/{rewritten}.json"
        )
        authorization.parent.mkdir(parents=True, exist_ok=True)
        authorization.write_text(json.dumps({
            "accepted_test_factory_sha": "a" * 40,
            "accepted_test_receipt_sha256": repair["receipt_sha256"],
            "accepted_test_run_id": "accepted-late-test",
            "base": base,
            "branch": "ticket/T-110",
            "factory_sha": "b" * 40,
            "head": rewritten,
            "head_tree": previous["head_tree"],
            "mode": "accepted-push-history-normalization",
            "passport_sha256": previous["passport_sha256"],
            "previous_head": old_head,
            "previous_tree": previous["head_tree"],
            "repository": "nysa-company/relay-factory",
            "route_plan_sha256": previous["route_plan_sha256"],
            "schema": PASSPORT.NORMALIZATION_SCHEMA,
            "state": "Blocked-Escalated",
            "ticket": "T-110",
        }, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        run("git", "add", ".", cwd=protected)
        run("git", "commit", "-qm", "authorize exact T-100 normalization", cwd=protected)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=protected)
        run(
            "git", "push", "-q", "--force-with-lease=refs/heads/ticket/T-110:" + old_head,
            "origin", rewritten + ":refs/heads/ticket/T-110", cwd=self.product,
        )
        run("git", "fetch", "-q", "origin", "main", cwd=self.product)

        migrated = PASSPORT.migrate(self.passport_args, secret)
        self.assertEqual(migrated["head_sha"], rewritten)
        self.assertEqual(migrated["head_tree"], previous["head_tree"])
        self.assertEqual(migrated["route_plan_sha256"], previous["route_plan_sha256"])
        self.assertEqual(migrated["completed_role_evidence"], old_evidence)
        self.assertEqual(migrated["charge_records"], old_charges)
        self.assertRegex(
            migrated["migration_history"][-1]["rewrite_authorization_sha256"],
            r"^[0-9a-f]{64}$",
        )
        rewrites = [
            edge for edge in migrated["migration_history"]
            if "rewrite_authorization_sha256" in edge
        ]
        self.assertEqual(len(rewrites), 1)
        self.assertEqual(rewrites[0]["from_head_sha"], old_head)
        self.assertEqual(rewrites[0]["to_head_sha"], rewritten)
        self.assertEqual(rewrites[0]["from_factory_sha"], "b" * 40)
        self.assertEqual(rewrites[0]["to_factory_sha"], "b" * 40)
        self.assertEqual(
            rewrites[0]["from_route_plan_sha256"],
            rewrites[0]["to_route_plan_sha256"],
        )
        self.assertEqual(PASSPORT.migrate(self.passport_args, secret), migrated)
        exported = PASSPORT.export(self.passport_args, secret)
        self.assertEqual(exported["head_sha"], rewritten)
        self.assertEqual(exported["charge_records"], old_charges)

        consumed = PASSPORT.receipt(
            self.state_dir, "T-110", repair["receipt_sha256"]
        )
        current = PASSPORT.identity(self.passport_args)
        self.assertTrue(STATE.contract_block_head_in_lineage(
            self.state_args, consumed, exported
        ))
        tampered = dict(migrated)
        tampered["migration_history"] = [
            dict(edge) for edge in migrated["migration_history"]
        ]
        tampered["migration_history"][-1].pop(
            "rewrite_authorization_sha256"
        )
        self.assertFalse(PASSPORT.migrated_receipt_lineage(
            self.passport_args, tampered, consumed, current
        ))
        wrong_parent = dict(migrated)
        wrong_parent["migration_history"] = [
            dict(edge) for edge in migrated["migration_history"]
        ]
        wrong_parent["migration_history"][-1][
            "from_passport_file_sha256"
        ] = "0" * 64
        self.assertFalse(PASSPORT.migrated_receipt_lineage(
            self.passport_args, wrong_parent, consumed, current
        ))
        unchained = dict(migrated)
        unchained["migration_history"] = [
            dict(edge) for edge in migrated["migration_history"]
        ]
        unchained["migration_history"][-1]["from_head_sha"] = base
        self.assertFalse(PASSPORT.migrated_receipt_lineage(
            self.passport_args, unchained, consumed, current
        ))


if __name__ == "__main__":
    unittest.main()
