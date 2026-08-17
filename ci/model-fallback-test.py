#!/usr/bin/env python3
"""End-to-end regression for fallback preview and trusted handoff commit."""

import base64
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/model-fallback.py"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


ROUTER = module("fallback_test_router", ROOT / "scripts/model-router.py")
MANAGER = module("fallback_test_manager", ROOT / "scripts/model-manager.py")


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


class FallbackTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.repo = base / "ticket"
        self.remote = base / "remote.git"
        self.fetch_remote = base / "fetch.git"
        self.product = base / "product"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "--bare", self.remote], check=True)
        subprocess.run(["git", "init", "-q", "--bare", self.fetch_remote], check=True)
        subprocess.run(["git", "init", "-q", "-b", "ticket/T-1", self.repo], check=True)
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "user.email", "test@example.test")
        git(self.repo, "remote", "add", "origin", str(self.remote))

        catalog, routes, profiles, profile_map = ROUTER.load_policy()
        profile = profile_map["cursor-balanced-v2"]
        readiness = {
            route_id: {
                "adapter_version": "test-v1",
                "reason": "ok",
                "reported_identity": route["expected_reported_identity"],
                "state": "READY",
            }
            for route_id, route in routes.items()
            if route["enabled"]
        }
        resolution = ROUTER.resolve_policy(catalog, routes, profile, readiness)
        legacy = {
            "created_at": "2026-07-18T12:00:00Z",
            "kit_sha": "a" * 40,
            "resolution": resolution,
            "schema": "ticket-model-route-plan/v1",
            "ticket": "T-1",
        }
        legacy_raw = (ROUTER.canonical_json(legacy) + "\n").encode()
        journal = MANAGER.migrate_v1_plan(
            legacy_raw,
            "b" * 40,
            "a" * 40,
            "2026-07-18T12:01:00Z",
            catalog,
            routes,
            profile_map,
        )
        (self.repo / "factory/route-plans").mkdir(parents=True)
        (self.repo / "factory/tickets").mkdir()
        (self.repo / "src").mkdir()
        (self.repo / "factory/route-plans/T-1.json").write_text(
            ROUTER.canonical_json(journal) + "\n"
        )
        (self.repo / "factory/QUALIFICATION.json").write_text(json.dumps({
            "factory_sha": "a" * 40,
            "generation": 1,
            "schema": "nysa.software-factory.qualification/v2",
            "tickets": ["T-1"],
        }))
        (self.repo / "factory/tickets/T-1.md").write_text(
            "State: Building\nKit-SHA: " + "a" * 40 + "\n"
        )
        (self.repo / "src/app.txt").write_text("before\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "ticket start")
        git(self.repo, "push", "-u", "origin", "ticket/T-1")
        git(self.repo, "config", "remote.origin.pushurl", str(self.remote))
        git(self.repo, "config", "remote.origin.url", str(self.fetch_remote))
        self.head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.head)

        failed = resolution["selections"]["builder"]
        readiness[failed["route_id"]]["state"] = "UNAVAILABLE"
        readiness[failed["route_id"]]["reason"] = "credits_exhausted"
        for value in readiness.values():
            value["adapter_version"] = "tést-v1"
        self.readiness = base / "readiness.json"
        self.readiness.write_text(ROUTER.canonical_json(readiness) + "\n")
        runs = self.product / "factory/runs"
        runs.mkdir(parents=True)
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        manifest = {
            "accounting_schema": "1",
            "accounting_state": "completed",
            "adapter": failed["adapter"],
            "adapter_version": "test-v1",
            "cost_basis": "test_fixture",
            "effective_cost": "1.00",
            "exit_status": "75",
            "go_issued": "1",
            "kit_sha": "a" * 40,
            "model_id": failed["selection_id"],
            "policy_hash": resolution["policy_hash"],
            "phase": "completed",
            "prompt_version": "1",
            "provider_family": failed["provider_family"],
            "reserved_usd": "2.00",
            "role": "builder",
            "role_exit": "provider_failed",
            "role_branch_before": "ticket/T-1",
            "role_head_before": self.head,
            "role_remote_before": self.head,
            "route_id": failed["route_id"],
            "run_id": "run-failed-1",
            "selection_reason": "test_fixture",
            "started_at": now,
            "task_submitted": "1",
            "terminal_at": now,
            "ticket": "T-1",
            "turns": "1",
        }
        (runs / "run-failed-1.meta").write_text(
            "".join(f"{key}={value}\n" for key, value in sorted(manifest.items()))
        )
        (self.product / "factory/ledger.csv").write_text(
            "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,"
            "exit_status,run_id,provider_family,model_id,selection_reason,"
            "cost_basis,adapter_version\n"
        )
        (self.product / "factory/runtime-ledger.csv").write_text(
            "ticket,run_id,exit_status\nT-1,stale-runtime-view,0\n"
        )
        (self.repo / "src/app.txt").write_text("partial handoff\n")

    def tearDown(self):
        self.temp.cleanup()

    def command(
        self, action, *extra, check=True, reason="credits_exhausted", environment=None,
        failed_run="run-failed-1", input_text=None,
    ):
        result = subprocess.run(
            [
                sys.executable, str(SCRIPT), action,
                "--workdir", str(self.repo),
                "--factory-root", str(self.product),
                "--project", "test",
                "--ticket", "T-1",
                "--failed-run", failed_run,
                "--reason", reason,
                "--readiness", str(self.readiness),
                "--remote", str(self.remote),
                *extra,
            ],
            text=True,
            input=input_text,
            capture_output=True,
            env={**os.environ, **(environment or {})},
        )
        if result.returncode and check:
            self.fail(result.stderr)
        if result.returncode:
            return result
        return json.loads(result.stdout)

    def test_cancelled_attempt_is_eligible_for_same_stage_fallback(self):
        path = self.product / "factory/runs/run-failed-1.meta"
        values = {}
        for line in path.read_text().splitlines():
            key, value = line.split("=", 1)
            values[key] = value
        values.update({
            "accounting_state": "cancelled_conservative",
            "cancellation_reason": "budget_exhausted",
            "phase": "cancelled_conservative",
            "role_exit": "cancelled",
        })
        path.write_text("".join(
            f"{key}={value}\n" for key, value in sorted(values.items())
        ))
        preview = self.command("preview", reason="budget_exhausted")
        self.assertEqual(preview["reason"], "budget_exhausted")
        self.assertEqual(preview["failed_run_id"], "run-failed-1")

    def test_github_https_fallback_refuses_without_explicit_credential(self):
        before_head = git(self.repo, "rev-parse", "HEAD")
        before_status = git(self.repo, "status", "--porcelain")
        result = self.command(
            "preview",
            "--remote",
            "https://github.com/nysa-company/relay-factory.git",
            check=False,
            environment={
                "GH_CONFIG_DIR": "/tmp/untrusted-config",
                "GH_TOKEN": "ambient-token-must-be-ignored",
                "GITHUB_TOKEN": "ambient-token-must-be-ignored",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("github_credential_unavailable", result.stderr)
        self.assertNotIn("ambient-token-must-be-ignored", result.stderr)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before_head)
        self.assertEqual(git(self.repo, "status", "--porcelain"), before_status)

    def test_github_https_preview_scopes_credential_to_remote_reads(self):
        base = self.repo.parent
        tools = base / "tools"
        tools.mkdir()
        trace = base / "git-network.trace"
        helper = (tools / "gh").resolve()
        helper.write_text("#!/bin/sh\nexit 0\n")
        helper.chmod(0o700)
        wrapper = tools / "git"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "args = sys.argv[1:]\n"
            "auth_names = ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', "
            "'GITHUB_ENTERPRISE_TOKEN', 'GH_HOST')\n"
            "url = os.environ['TEST_GITHUB_URL']\n"
            "ssh = os.environ['TEST_GITHUB_SSH']\n"
            "network = 'ls-remote' in args and (url in args or ssh in args)\n"
            "if url in args and network:\n"
            "    assert all(name not in os.environ for name in auth_names)\n"
            "    assert os.environ['GH_CONFIG_DIR'] == os.path.join(os.environ['HOME'], '.config', 'gh')\n"
            "    assert any('credential.https://github.com.helper=!' in x for x in args)\n"
            "    if os.environ.get('TEST_GITHUB_AUTH_FAIL'):\n"
            "        raise SystemExit(1)\n"
            "    args = [os.environ['TEST_LOCAL_REMOTE'] if x == url else x for x in args]\n"
            "    with open(os.environ['TEST_GIT_TRACE'], 'a') as handle:\n"
            "        handle.write('authenticated-remote-read\\n')\n"
            "elif ssh in args and network:\n"
            "    assert all(name not in os.environ for name in (*auth_names, 'GH_CONFIG_DIR'))\n"
            "    args = [os.environ['TEST_LOCAL_REMOTE'] if x == ssh else x for x in args]\n"
            "    with open(os.environ['TEST_GIT_TRACE'], 'a') as handle:\n"
            "        handle.write('credential-free-ssh-read\\n')\n"
            "else:\n"
            "    assert all(name not in os.environ for name in (*auth_names, 'GH_CONFIG_DIR'))\n"
            "os.execv('/usr/bin/git', ['/usr/bin/git', *args])\n"
        )
        wrapper.chmod(0o700)
        url = "https://github.com/nysa-company/relay-factory.git"
        ssh_url = "git@github.com:nysa-company/relay-factory.git"
        environment = {
            "PATH": f"{tools}:/usr/bin:/bin",
            "TEST_GITHUB_URL": url,
            "TEST_GITHUB_SSH": ssh_url,
            "TEST_LOCAL_REMOTE": str(self.remote),
            "TEST_GIT_TRACE": str(trace),
            "GH_CONFIG_DIR": "/tmp/untrusted-config",
            "GH_ENTERPRISE_TOKEN": "ambient-enterprise-token",
            "GH_HOST": "untrusted.example",
            "GH_TOKEN": "ambient-token",
            "GITHUB_ENTERPRISE_TOKEN": "ambient-github-enterprise-token",
            "GITHUB_TOKEN": "ambient-github-token",
        }
        value = self.command(
            "preview",
            "--remote",
            url,
            "--github-helper",
            str(helper),
            environment=environment,
        )
        self.assertEqual(value["schema"], "ticket-model-fallback-preview/v1")
        self.assertEqual(
            trace.read_text().splitlines(),
            ["authenticated-remote-read", "authenticated-remote-read"],
        )
        self.assertNotIn("ambient-token", json.dumps(value))

        before_head = git(self.repo, "rev-parse", "HEAD")
        refused = self.command(
            "preview",
            "--remote",
            url,
            "--github-helper",
            str(helper),
            environment={**environment, "TEST_GITHUB_AUTH_FAIL": "1"},
            check=False,
        )
        self.assertIn("github_https_authentication_failed", refused.stderr)
        self.assertNotIn("ambient-token", refused.stderr)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before_head)

        ssh_value = self.command(
            "preview", "--remote", ssh_url, environment=environment
        )
        self.assertEqual(ssh_value["schema"], "ticket-model-fallback-preview/v1")
        self.assertEqual(
            trace.read_text().splitlines(),
            [
                "authenticated-remote-read",
                "authenticated-remote-read",
                "credential-free-ssh-read",
                "credential-free-ssh-read",
            ],
        )

    def test_preview_then_apply_commits_partial_work_and_journal_once(self):
        preview = self.command("preview")
        journal_path = self.repo / "factory/route-plans/T-1.json"
        initial_journal = journal_path.read_bytes()
        approval = Path(self.temp.name) / "approval.json"
        approval.write_text(json.dumps({
            "approval_hash": preview["approval_hash"],
            "receipt_sha256": "c" * 64,
            "failed_run_id": "run-failed-1",
            "nonce": preview["nonce"],
            "operator_id": "operator-user-1",
            "reason": "credits_exhausted",
            "schema": "model-fallback-receipt-approval/v1",
        }))
        applied = self.command("apply", "--approval", str(approval))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), applied["commit_sha"])
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(
            git(self.repo, "show", "HEAD:src/app.txt"), "partial handoff"
        )
        journal = json.loads(
            git(self.repo, "show", "HEAD:factory/route-plans/T-1.json")
        )
        journal_raw = git(
            self.repo, "show", "HEAD:factory/route-plans/T-1.json"
        )
        self.assertIn("tést-v1", journal_raw)
        self.assertEqual(journal_raw, ROUTER.canonical_json(journal))
        self.assertEqual(len(journal["revisions"]), 2)
        self.assertEqual(
            journal["revisions"][-1]["revision_hash"], applied["revision_hash"]
        )
        # Simulate a crash after update-ref but before index/worktree journal
        # publication. Retry must recognize and complete the committed handoff.
        git(self.repo, "read-tree", "HEAD^")
        journal_path.write_bytes(initial_journal)
        recovered = self.command("apply", "--approval", str(approval))
        self.assertTrue(recovered["recovered"])
        self.assertEqual(recovered["commit_sha"], applied["commit_sha"])
        consumed_recovery = self.command("recover")
        self.assertTrue(consumed_recovery["recovered"])
        self.assertEqual(
            consumed_recovery["approval_receipt"]["receipt_sha256"],
            "c" * 64,
        )
        self.assertEqual(
            len(json.loads(
                git(self.repo, "show", "HEAD:factory/route-plans/T-1.json")
            )["revisions"]),
            2,
        )

    def test_qualification_apply_uses_direct_cli_once(self):
        qualification = self.product / "factory/QUALIFICATION.json"
        qualification.write_text(json.dumps({
            "budget_usd": "100.000000",
            "capacity": 3,
            "contract_version": "1.8.0",
            "factory_sha": "a" * 40,
            "generation": 1,
            "per_run_budget_usd": "2.000000",
            "per_ticket_budget_usd": "25.000000",
            "schema": "nysa.software-factory.qualification/v2",
            "target_done": 3,
            "tickets": ["T-1", "T-2", "T-3"],
        }))
        git(self.product, "init", "-q", "-b", "main")
        git(self.product, "config", "user.name", "Test")
        git(self.product, "config", "user.email", "test@example.test")
        git(self.product, "add", ".")
        git(self.product, "commit", "-m", "ordinary qualification authority")
        environment = {
            "FACTORY_KIT_TRUST_SCOPE": "qualification-candidate",
            "FACTORY_QUALIFICATION_MANIFEST": str(qualification),
            "FACTORY_QUALIFICATION_PRODUCT_SHA": git(
                self.product, "rev-parse", "HEAD"
            ),
            "FACTORY_QUALIFICATION_PRODUCT_TREE": git(
                self.product, "rev-parse", "HEAD^{tree}"
            ),
            "FACTORY_RELEASE_SHA": "a" * 40,
            "FACTORY_ROOT": str(self.product),
        }
        initial = json.loads(
            (self.repo / "factory/route-plans/T-1.json").read_text()
        )
        reviewer_route = MANAGER.active_resolution(initial)["selections"]["reviewer"][
            "route_id"
        ]
        readiness = json.loads(self.readiness.read_text())
        readiness[reviewer_route].update({
            "reason": "model_unavailable",
            "state": "INVALID",
        })
        self.readiness.write_text(ROUTER.canonical_json(readiness) + "\n")

        applied = self.command("qualification-apply", environment=environment)
        journal = json.loads(
            git(self.repo, "show", "HEAD:factory/route-plans/T-1.json")
        )
        resolution = journal["revisions"][-1]["body"]["new_resolution"]
        self.assertEqual(resolution["future_roles"], ["builder"])
        self.assertEqual(resolution["selections"]["builder"]["adapter"], "codex")
        self.assertEqual(
            resolution["selections"]["reviewer"]["route_id"], reviewer_route
        )
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), applied["commit_sha"])
        recovered = self.command("qualification-apply", environment=environment)
        self.assertTrue(recovered["recovered"])
        self.assertEqual(recovered["commit_sha"], applied["commit_sha"])

    def test_qualification_fallback_is_scoped_to_failure_generation(self):
        first = self.command("qualification-apply")
        route = self.repo / "factory/route-plans/T-1.json"
        journal = json.loads(route.read_text())
        catalog, routes, _profiles, profile_map = ROUTER.load_policy()
        next_kit = "f" * 40
        migrated = MANAGER.migrate_v2_journal(
            journal,
            first["commit_sha"],
            next_kit,
            "2026-07-18T12:02:00Z",
            catalog,
            routes,
            profile_map,
        )
        route.write_text(ROUTER.canonical_json(migrated) + "\n")
        ticket = self.repo / "factory/tickets/T-1.md"
        ticket.write_text(ticket.read_text().replace("a" * 40, next_kit))
        qualification = self.repo / "factory/QUALIFICATION.json"
        value = json.loads(qualification.read_text())
        value.update(factory_sha=next_kit, generation=2)
        qualification.write_text(json.dumps(value))
        git(self.repo, "add", "factory/route-plans/T-1.json", "factory/tickets/T-1.md", "factory/QUALIFICATION.json")
        git(self.repo, "commit", "-m", "migrate stale fallback generation")
        git(self.repo, "push", "origin", "ticket/T-1")
        git(self.repo, "update-ref", "refs/remotes/origin/main", git(self.repo, "rev-parse", "HEAD"))
        head = git(self.repo, "rev-parse", "HEAD")

        active = MANAGER.active_resolution(migrated)
        failed = active["selections"]["reviewer"]
        readiness = json.loads(self.readiness.read_text())
        readiness[failed["route_id"]].update({
            "reason": "provider_unavailable",
            "state": "UNAVAILABLE",
        })
        self.readiness.write_text(ROUTER.canonical_json(readiness) + "\n")
        old = self.product / "factory/runs/run-failed-1.meta"
        values = dict(line.split("=", 1) for line in old.read_text().splitlines())
        values.update({
            "adapter": failed["adapter"],
            "adapter_version": readiness[failed["route_id"]]["adapter_version"],
            "kit_sha": next_kit,
            "model_id": failed["selection_id"],
            "policy_hash": active["policy_hash"],
            "provider_family": failed["provider_family"],
            "role": "reviewer",
            "role_head_before": head,
            "role_remote_before": head,
            "route_id": failed["route_id"],
            "run_id": "run-reviewer-2",
        })
        (old.parent / "run-reviewer-2.meta").write_text(
            "".join(f"{key}={item}\n" for key, item in sorted(values.items()))
        )
        second = self.command(
            "qualification-apply",
            failed_run="run-reviewer-2",
            reason="provider_unavailable",
        )
        final = json.loads(route.read_text())
        fallbacks = [
            item for item in final["revisions"]
            if item["body"].get("kind") == "fallback"
        ]
        self.assertEqual(len(fallbacks), 2)
        self.assertEqual(
            fallbacks[-1]["body"]["approval_receipt"]["failed_run_id"],
            "run-reviewer-2",
        )
        replay = self.command(
            "qualification-apply",
            failed_run="run-reviewer-2",
            reason="provider_unavailable",
        )
        self.assertTrue(replay["recovered"])
        self.assertEqual(replay["commit_sha"], second["commit_sha"])
        self.assertEqual(
            len([
                item for item in json.loads(route.read_text())["revisions"]
                if item["body"].get("kind") == "fallback"
            ]),
            2,
        )

    def test_qualification_attempt_limit_is_candidate_scoped(self):
        current = self.product / "factory/runs/run-failed-1.meta"
        historical = self.product / "factory/runs/run-000-historical.meta"
        historical.write_text(
            current.read_text()
            .replace("run_id=run-failed-1", "run_id=run-000-historical")
            .replace("kit_sha=" + "a" * 40, "kit_sha=" + "f" * 40)
            .replace(
                "started_at=2026-",
                "started_at=2025-",
            )
            .replace(
                "terminal_at=2026-",
                "terminal_at=2025-",
            )
        )
        applied = self.command("qualification-apply")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), applied["commit_sha"])

    def test_fallback_reduces_authoritative_accounting_not_runtime_view(self):
        preview = self.command("preview")
        self.assertEqual(preview["failed_run_id"], "run-failed-1")

    def test_builder_handoff_accepts_only_its_own_ticket_log(self):
        ticket = self.repo / "factory/tickets/T-1.md"
        original = ticket.read_text()
        ticket.write_text(original + "Builder root cause: scoped failure.\n")
        preview = self.command("preview")
        self.assertEqual(preview["failed_run_id"], "run-failed-1")

        ticket.write_text(original)
        sibling = self.repo / "factory/tickets/T-2.md"
        sibling.write_text("State: Backlog\n")
        result = self.command("preview", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "path is forbidden for builder: factory/tickets/T-2.md",
            result.stderr,
        )

    def test_qualification_apply_uses_sealed_local_successor_manifest(self):
        protected = self.repo / "factory/QUALIFICATION.json"
        value = json.loads(protected.read_text())
        value["factory_sha"] = "e" * 40
        protected.write_text(json.dumps(value))
        git(self.repo, "add", "factory/QUALIFICATION.json")
        git(self.repo, "commit", "-m", "unauthorized protected manifest")
        git(
            self.repo, "update-ref", "refs/remotes/origin/main",
            git(self.repo, "rev-parse", "HEAD"),
        )
        git(self.repo, "reset", "--hard", self.head)
        (self.repo / "src/app.txt").write_text("partial handoff\n")

        qualification = self.product / "factory/QUALIFICATION.json"
        qualification.write_text(json.dumps({
            "budget_usd": "300.000000",
            "capacity": 3,
            "contract_version": "1.8.0",
            "factory_sha": "f" * 40,
            "generation": 1,
            "mode": "successor",
            "per_run_budget_usd": "10.000000",
            "per_ticket_budget_usd": "100.000000",
            "schema": "nysa.software-factory.qualification/v2",
            "source_factory_sha": "a" * 40,
            "target_done": 3,
            "tickets": ["T-1", "T-2", "T-3"],
        }))
        git(self.product, "init", "-q", "-b", "main")
        git(self.product, "config", "user.name", "Test")
        git(self.product, "config", "user.email", "test@example.test")
        git(self.product, "add", ".")
        git(self.product, "commit", "-m", "local qualification authority")
        environment = {
            "FACTORY_KIT_TRUST_SCOPE": "qualification-candidate",
            "FACTORY_QUALIFICATION_MANIFEST": str(qualification),
            "FACTORY_QUALIFICATION_PRODUCT_SHA": git(
                self.product, "rev-parse", "HEAD"
            ),
            "FACTORY_QUALIFICATION_PRODUCT_TREE": git(
                self.product, "rev-parse", "HEAD^{tree}"
            ),
            "FACTORY_RELEASE_SHA": "f" * 40,
            "FACTORY_ROOT": str(self.product),
        }
        foreign_manifest = self.product.parent / "qualification-link.json"
        foreign_manifest.symlink_to(qualification)
        refused = self.command(
            "qualification-apply", check=False,
            environment={
                **environment,
                "FACTORY_QUALIFICATION_MANIFEST": str(foreign_manifest),
            },
        )
        self.assertIn("sealed qualification authority is invalid", refused.stderr)

        applied = self.command("qualification-apply", environment=environment)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), applied["commit_sha"])

        route = self.repo / "factory/route-plans/T-1.json"
        journal = json.loads(route.read_text())
        catalog, routes, _profiles, profile_map = ROUTER.load_policy()
        migrated = MANAGER.migrate_v2_journal(
            journal,
            applied["commit_sha"],
            "f" * 40,
            "2026-07-18T12:02:00Z",
            catalog,
            routes,
            profile_map,
        )
        route.write_text(ROUTER.canonical_json(migrated) + "\n")
        ticket = self.repo / "factory/tickets/T-1.md"
        ticket.write_text(ticket.read_text().replace("a" * 40, "f" * 40))
        git(self.repo, "add", "factory/route-plans/T-1.json", "factory/tickets/T-1.md")
        git(self.repo, "commit", "-m", "migrate fallback route")
        git(self.repo, "push", "origin", "ticket/T-1")

        recovered = self.command("qualification-apply", environment=environment)
        self.assertTrue(recovered["recovered"])
        self.assertEqual(recovered["commit_sha"], git(self.repo, "rev-parse", "HEAD"))

        (self.product / "qualification-rotation.txt").write_text("new receipt\n")
        git(self.product, "add", "qualification-rotation.txt")
        git(self.product, "commit", "-m", "rotate product authority")
        rotated = {
            **environment,
            "FACTORY_QUALIFICATION_PRODUCT_SHA": git(
                self.product, "rev-parse", "HEAD"
            ),
            "FACTORY_QUALIFICATION_PRODUCT_TREE": git(
                self.product, "rev-parse", "HEAD^{tree}"
            ),
        }
        refused = self.command(
            "qualification-apply", check=False, environment=rotated,
        )
        self.assertIn("qualification fallback authority changed", refused.stderr)

    def test_qualification_recovers_authenticated_cross_release_output(self):
        source = self.head
        git(self.repo, "add", "src/app.txt")
        git(self.repo, "commit", "-m", "preserve provider output")
        output = git(self.repo, "rev-parse", "HEAD")

        route = self.repo / "factory/route-plans/T-1.json"
        journal = json.loads(route.read_text())
        catalog, routes, _profiles, profile_map = ROUTER.load_policy()
        target = "f" * 40
        migrated = MANAGER.migrate_v2_journal(
            journal, source, target,
            dt.datetime.fromtimestamp(
                int(git(self.repo, "show", "-s", "--format=%ct", source)),
                dt.timezone.utc,
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            catalog, routes, profile_map,
        )
        route.write_text(ROUTER.canonical_json(migrated) + "\n")
        ticket = self.repo / "factory/tickets/T-1.md"
        ticket.write_text(ticket.read_text().replace("a" * 40, target))
        git(self.repo, "add", "factory/route-plans/T-1.json", "factory/tickets/T-1.md")
        git(self.repo, "commit", "-m", "migrate preserved output")
        recovery = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "origin", "ticket/T-1")

        authority = self.repo.parent / "authority"
        git(self.repo, "worktree", "add", "-q", "-b", "authority", str(authority), recovery)
        (authority / "factory/PROJECT.env").write_text(
            "GH_REPO=nysa-company/nysa-app\nTICKET_BRANCH_PREFIX=ticket/\n"
        )
        qualification = authority / "factory/QUALIFICATION.json"
        qualification.write_text(json.dumps({
            "budget_usd": "300.000000",
            "capacity": 3,
            "contract_version": "1.8.0",
            "factory_sha": target,
            "generation": 2,
            "mode": "successor",
            "per_run_budget_usd": "10.000000",
            "per_ticket_budget_usd": "100.000000",
            "schema": "nysa.software-factory.qualification/v2",
            "source_factory_sha": "a" * 40,
            "target_done": 3,
            "tickets": ["T-1", "T-2", "T-3"],
        }))
        authorization_path = (
            authority / f"factory/migrations/inflight-release/{target}.json"
        )
        authorization_path.parent.mkdir(parents=True)

        def seal(authorized_head):
            authorization_path.write_text(json.dumps({
                "repository": "nysa-company/nysa-app",
                "schema": "nysa.software-factory.inflight-release-authorization/v2",
                "source_kit_sha": "a" * 40,
                "target_kit_sha": target,
                "tickets": [{
                    "branch": "ticket/T-1",
                    "head": authorized_head,
                    "source_kit_sha": "a" * 40,
                    "state": "Building",
                    "ticket": "T-1",
                }],
            }))
            git(authority, "add", "factory")
            git(authority, "commit", "-m", "seal successor authority")
            return {
                "FACTORY_KIT_TRUST_SCOPE": "qualification-candidate",
                "FACTORY_QUALIFICATION_MANIFEST": str(qualification),
                "FACTORY_QUALIFICATION_PRODUCT_SHA": git(authority, "rev-parse", "HEAD"),
                "FACTORY_QUALIFICATION_PRODUCT_TREE": git(authority, "rev-parse", "HEAD^{tree}"),
                "FACTORY_RELEASE_SHA": target,
                "FACTORY_ROOT": str(authority),
            }

        bad = self.command(
            "qualification-apply", check=False, environment=seal(source),
        )
        self.assertIn("historical qualification handoff is invalid", bad.stderr)
        environment = seal(output)
        applied = self.command("qualification-apply", environment=environment)
        current = json.loads(route.read_text())
        body = current["revisions"][-1]["body"]
        proof = body["approval_receipt"]["historical_handoff"]
        self.assertEqual(proof["source_head"], source)
        self.assertEqual(proof["authorized_head"], output)
        self.assertEqual(proof["recovery_head"], recovery)
        self.assertEqual(body["approved_snapshot_digest"], proof["snapshot_digest"])
        self.assertEqual(
            git(self.repo, "diff-tree", "--no-commit-id", "--name-only", "-r", applied["commit_sha"]),
            "factory/route-plans/T-1.json",
        )

        recovered = self.command("qualification-apply", environment=environment)
        self.assertTrue(recovered["recovered"])
        self.assertEqual(recovered["commit_sha"], applied["commit_sha"])

    def test_sealed_successor_refuses_unbound_source_factory(self):
        qualification = self.product / "factory/QUALIFICATION.json"
        qualification.write_text(json.dumps({
            "budget_usd": "300.000000",
            "capacity": 3,
            "contract_version": "1.8.0",
            "factory_sha": "f" * 40,
            "generation": 1,
            "mode": "successor",
            "per_run_budget_usd": "10.000000",
            "per_ticket_budget_usd": "100.000000",
            "schema": "nysa.software-factory.qualification/v2",
            "source_factory_sha": "b" * 40,
            "target_done": 3,
            "tickets": ["T-1", "T-2", "T-3"],
        }))
        git(self.product, "init", "-q", "-b", "main")
        git(self.product, "config", "user.name", "Test")
        git(self.product, "config", "user.email", "test@example.test")
        git(self.product, "add", ".")
        git(self.product, "commit", "-m", "mismatched successor authority")
        before = git(self.repo, "rev-parse", "HEAD")

        result = self.command("qualification-apply", check=False, environment={
            "FACTORY_KIT_TRUST_SCOPE": "qualification-candidate",
            "FACTORY_QUALIFICATION_MANIFEST": str(qualification),
            "FACTORY_QUALIFICATION_PRODUCT_SHA": git(
                self.product, "rev-parse", "HEAD"
            ),
            "FACTORY_QUALIFICATION_PRODUCT_TREE": git(
                self.product, "rev-parse", "HEAD^{tree}"
            ),
            "FACTORY_RELEASE_SHA": "f" * 40,
            "FACTORY_ROOT": str(self.product),
        })

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "sealed qualification manifest does not authorize fallback",
            result.stderr,
        )
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before)

    def test_qualification_apply_migrates_initial_v1_plan(self):
        path = self.repo / "factory/route-plans/T-1.json"
        journal = json.loads(path.read_text())
        legacy = base64.b64decode(
            journal["revisions"][0]["body"]["legacy_plan_b64"]
        )
        path.write_bytes(legacy)
        git(self.repo, "add", str(path.relative_to(self.repo)))
        git(self.repo, "commit", "-m", "restore initial route plan")
        git(self.repo, "push", "origin", "ticket/T-1")
        head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", head)
        manifest = self.product / "factory/runs/run-failed-1.meta"
        manifest.write_text(
            manifest.read_text()
            .replace(f"role_head_before={self.head}", f"role_head_before={head}")
            .replace(f"role_remote_before={self.head}", f"role_remote_before={head}")
        )

        applied = self.command("qualification-apply")
        migrated = json.loads(
            git(self.repo, "show", "HEAD:factory/route-plans/T-1.json")
        )
        self.assertEqual(
            [item["body"]["kind"] for item in migrated["revisions"]],
            ["migration", "fallback"],
        )
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), applied["commit_sha"])

    def test_qualification_apply_refuses_a_second_role_attempt(self):
        second = self.product / "factory/runs/run-failed-2.meta"
        second.write_text(
            (self.product / "factory/runs/run-failed-1.meta").read_text()
            .replace("run_id=run-failed-1", "run_id=run-failed-2")
        )
        result = self.command("qualification-apply", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed run is not the latest unique ticket attempt", result.stderr)

    def test_handoff_preserves_role_commits_and_remaining_dirty_work(self):
        git(self.repo, "add", "src/app.txt")
        git(self.repo, "commit", "-m", "partial builder progress")
        role_commit = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "src/app.txt").write_text("remaining handoff\n")

        preview = self.command("preview")
        approval = Path(self.temp.name) / "approval-with-commit.json"
        approval.write_text(json.dumps({
            "approval_hash": preview["approval_hash"],
            "receipt_sha256": "d" * 64,
            "failed_run_id": "run-failed-1",
            "nonce": preview["nonce"],
            "operator_id": "operator-user-1",
            "reason": "credits_exhausted",
            "schema": "model-fallback-receipt-approval/v1",
        }))
        applied = self.command("apply", "--approval", str(approval))

        self.assertEqual(git(self.repo, "rev-parse", "HEAD^"), role_commit)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), applied["commit_sha"])
        self.assertEqual(
            git(self.repo, "show", "HEAD:src/app.txt"), "remaining handoff"
        )
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_conservatively_accounted_completed_failure_is_eligible(self):
        manifest = self.product / "factory/runs/run-failed-1.meta"
        values = {}
        for line in manifest.read_text().splitlines():
            key, value = line.split("=", 1)
            values[key] = value
        values["accounting_state"] = "abandoned_conservative"
        values["phase"] = "completed"
        manifest.write_text(
            "".join(f"{key}={value}\n" for key, value in sorted(values.items()))
        )
        preview = self.command("preview")
        self.assertEqual(preview["failed_run_id"], "run-failed-1")

    def test_pre_submission_failure_is_ineligible(self):
        manifest = self.product / "factory/runs/run-failed-1.meta"
        manifest.write_text(
            manifest.read_text().replace("task_submitted=1", "task_submitted=0")
        )
        result = self.command("preview", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "not an eligible terminal provider failure",
            result.stderr,
        )


if __name__ == "__main__":
    unittest.main()
