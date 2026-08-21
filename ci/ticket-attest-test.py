#!/usr/bin/env python3
"""Network-free trusted ticket attestation regressions."""

import argparse
import base64
from datetime import timedelta
import json
import hashlib
import hmac
import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ticket-attest.py"
KIT_SHA = "a" * 40
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

SPEC = importlib.util.spec_from_file_location("ticket_attest", SCRIPT)
TICKET_ATTEST = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TICKET_ATTEST)

sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import operator_receipt  # noqa: E402


def command(*args, cwd=None, env=None, check=True):
    result = subprocess.run(
        args, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if check and result.returncode:
        raise AssertionError(result.stderr)
    return result


def historical_pre_go_orphan(path):
    values = {
        "run_id": path.stem, "phase": "resolved", "accounting_schema": "",
        "accounting_state": "", "reserved_usd": "10", "go_issued": "0",
        "task_submitted": "0", "started_at": "2026-08-19T16:31:58Z",
        "terminal_at": "", "prompt_version": "6", "turns": "0",
        "effective_cost": "", "exit_status": "", "cost_basis": "",
        "ticket": "T-323", "role": "planner", "adapter": "codex",
        "provider_family": "openai", "model_id": "gpt-test",
        "selection_reason": "fallback_ready", "adapter_version": "test",
        "pid": "", "pgid": "", "process_start": "", "role_exit": "",
        "output_sha256": "", "progress_events": "",
        "progress_journal_sha256": "", "timeout_kind": "",
        "terminal_reason_code": "", "cancellation_reason": "",
        "cancellation_preview_hash": "", "updated_at": "2026-08-19T16:32:19Z",
    }
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))


class LauncherContractTests(unittest.TestCase):
    def test_launcher_binds_contract_version_to_ticket_attest_helper(self):
        launcher = (
            ROOT / "scripts/factory-launch"
        ).read_text(encoding="utf-8")
        ticket_attest = launcher.split(
            "\n  ticket-attest)\n", 1,
        )[1].split("\n  project-ledger)\n", 1)[0]
        self.assertIn(
            '"FACTORY_CONTRACT_VERSION=$CONTRACT_VERSION"',
            ticket_attest,
        )

    def test_launcher_confines_emergency_closeout_to_contract_18(self):
        launcher = (
            ROOT / "scripts/factory-launch"
        ).read_text(encoding="utf-8")
        ticket_attest = launcher.split(
            "\n  ticket-attest)\n", 1,
        )[1].split("\n  project-ledger)\n", 1)[0]
        self.assertIn('"$CONTRACT_VERSION" == "1.8.0"', ticket_attest)
        self.assertIn('"$ATTEST_EMERGENCY" -eq 0', ticket_attest)
        self.assertIn('"FACTORY_CONTROLLER_STATE_DIR=$CONTROLLER_STATE_DIR"', ticket_attest)
        self.assertIn('ATTEST_ACTION" == "emergency-apply"', ticket_attest)


class TicketAttestTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="ticket-attest-test."))
        self.product = self.temp / "product"
        self.remote = self.temp / "product.git"
        self.bin = self.temp / "bin"
        self.state = self.temp / "gh.json"
        self.product.mkdir()
        self.bin.mkdir()
        command("git", "init", "--bare", "-q", str(self.remote))
        command("git", "init", "-q", "-b", "main", cwd=self.product)
        command("git", "config", "user.name", "test", cwd=self.product)
        command("git", "config", "user.email", "test@example.com", cwd=self.product)
        command("git", "remote", "add", "origin", str(self.remote), cwd=self.product)
        (self.product / "factory/tickets").mkdir(parents=True)
        (self.product / "factory/runs").mkdir()
        (self.product / "factory/route-plans").mkdir()
        (self.product / "factory/PROJECT.env").write_text(
            "GH_REPO=acme/widget\nDONE_REQUIRED_CHECKS=ci,deploy-production\n"
            "AUTO_MERGE_METHOD=squash\nTEST_PATHS=tests/\n"
        )
        (self.product / ".gitignore").write_text(
            "factory/runs/\nfactory/runtime-ledger.csv\nfactory/operator-map.json\n"
        )
        ledger_header = (
            "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,"
            "run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version\n"
        )
        (self.product / "factory/ledger.csv").write_text(ledger_header)
        (self.product / "factory/KIT_PIN").write_text(
            command("git", "-C", str(ROOT), "rev-parse", "HEAD").stdout.strip() + "\n"
        )
        (self.product / "factory/QUALIFICATION.json").write_text("{}\n")
        selection = {
            "account_route_id": "test-account",
            "adapter": "mock",
            "adapter_version": "1",
            "effort": "medium",
            "gateway_id": "direct",
            "inference_provider_id": "test-provider",
            "provider_family": "anthropic",
            "reported_identity": "mock",
            "role": "",
            "route_id": "mock-route",
            "selection_id": "mock",
            "transport": "test",
        }
        selections = {
            role: {**selection, "role": role}
            for role in (
                "planner", "spec-linter", "test-author", "builder", "reviewer", "narrator",
            )
        }
        route_plan = {
            "created_at": "2026-07-17T11:00:00Z",
            "kit_sha": KIT_SHA,
            "resolution": {
                "catalog_hash": "b" * 64,
                "policy_hash": "d" * 64,
                "portfolio_id": "test-portfolio",
                "profile_hash": "c" * 64,
                "profile_id": "test-profile",
                "profile_version": 1,
                "schema": "model-resolution-plan/v1",
                "selections": selections,
            },
            "schema": "ticket-model-route-plan/v1",
            "ticket": "T-700",
        }
        (self.product / "factory/route-plans/T-700.json").write_text(
            json.dumps(route_plan, indent=2, sort_keys=True) + "\n"
        )
        (self.product / "factory/tickets/T-700.md").write_text(self.ticket("Review"))
        self.commit("base")
        command("git", "push", "-q", "-u", "origin", "main", cwd=self.product)
        command("git", "switch", "-q", "-c", "ticket/T-700", cwd=self.product)
        (self.product / "app.txt").write_text("reviewed code\n")
        self.commit("implementation")
        self.reviewed = self.head()

        (self.product / "factory/tickets/T-700.md").write_text(
            self.ticket("Review") + "\nreviewer round 1: APPROVE\n"
        )
        (self.product / "factory/tickets/T-700-bundle.md").write_text(
            "# Evidence bundle\n"
            "## What this does\nSafe change.\n"
            "## Preview\nLocal preview.\n"
            "## Screenshots\nNo visual change.\n"
            "## Acceptance criteria\nAll pass.\n"
            "## Risk\nLow.\n"
            "## Cost\n1 USD.\n"
            "## Rollback\nRevert PR.\n"
            "Approve to merge, or send back with what is wrong?\n"
        )
        self.commit("narrator bundle")
        command("git", "push", "-q", "-u", "origin", "ticket/T-700", cwd=self.product)
        self.write_runs()
        self.install_fake_gh()
        self.write_state()
        # operator_receipt refuses unresolved paths (macOS /var symlinks).
        self.controller = Path(os.path.realpath(self.temp)) / "controller"
        self.controller.mkdir(mode=0o700)
        self.controller.chmod(0o700)
        self.env = dict(os.environ)
        self.env.update({
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FACTORY_ROOT": str(self.product),
            "FACTORY_CERTIFIED_PRODUCT_ORIGIN": str(self.remote),
            "FACTORY_RELEASE_SHA": KIT_SHA,
            "FACTORY_RELEASE_CONTRACT_VERSION": "1.8.0",
            "FACTORY_CONTROLLER_STATE_DIR": str(self.controller),
            "FACTORY_PROJECT": "example-product",
            "FAKE_GH_STATE": str(self.state),
            "FAKE_WORKDIR": str(self.product),
        })
        self.workdir = self.product

    def test_ls_remote_retries_one_transport_failure(self):
        failed = subprocess.CompletedProcess(["git"], 128, "", "transport failed")
        passed = subprocess.CompletedProcess(["git"], 0, "head\trefs/heads/main\n", "")
        with patch.object(TICKET_ATTEST, "run", side_effect=[failed, passed]) as call:
            result = TICKET_ATTEST.git(
                self.product, "ls-remote", "--heads", "origin", "refs/heads/main",
            )
        self.assertEqual(result.stdout, passed.stdout)
        self.assertEqual(call.call_count, 2)

    def test_post_push_pr_confirmation_waits_for_github_head_convergence(self):
        head = "b" * 40
        stale = {"headRefOid": "a" * 40}
        current = {"headRefOid": head}
        with (
            patch.object(TICKET_ATTEST, "exact_pr", side_effect=[stale, current]) as exact,
            patch.object(TICKET_ATTEST.time, "sleep") as sleep,
        ):
            result = TICKET_ATTEST.exact_pr_after_push(
                "acme/product", "ticket/T-700", head,
            )
        self.assertEqual(result, current)
        self.assertEqual(exact.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_successful_runs_use_canonical_worktree_ledger(self):
        canonical = self.temp / "canonical-product"
        canonical.mkdir()
        command("git", "init", "-q", "-b", "main", cwd=canonical)
        (canonical / "factory").mkdir()
        ledger = self.product / "factory/runtime-ledger.csv"
        (canonical / "factory/runtime-ledger.csv").write_bytes(ledger.read_bytes())
        ledger.write_text(ledger.read_text().splitlines()[0] + "\n")

        with patch.dict(os.environ, {"FACTORY_LEDGER": ""}):
            runs = TICKET_ATTEST.successful_runs(
                self.product, canonical, "T-700",
            )
        self.assertCountEqual([run["role"] for run in runs], ["reviewer", "narrator"])

    def test_terminal_finalize_records_basis_only_after_protected_validation(self):
        def terminal(*_args):
            return {"basis": "attested-done", "ticket": "T-700"}

        def git(*args, **_kwargs):
            output = "b" * 40 + "\n" if "rev-parse" in args else ""
            return subprocess.CompletedProcess([], 0, output, "")

        with (
            patch.object(TICKET_ATTEST, "protected_terminal", side_effect=terminal),
            patch.object(TICKET_ATTEST, "git", side_effect=git),
        ):
            result = TICKET_ATTEST.finalize_terminal(
                self.product, self.product, str(self.remote), "T-700", "attested-done"
            )

        self.assertEqual(
            result, {"basis": "attested-done", "protected_main": "b" * 40}
        )

    def test_terminal_finalize_refuses_when_protected_validation_fails(self):
        def git(*_args, **_kwargs):
            return subprocess.CompletedProcess([], 0, "", "")

        with (
            patch.object(TICKET_ATTEST, "git", side_effect=git),
            patch.object(
                TICKET_ATTEST, "protected_terminal",
                side_effect=TICKET_ATTEST.ValidationError("missing terminal"),
            ),
            self.assertRaisesRegex(TICKET_ATTEST.Refusal, "protected terminal"),
        ):
            TICKET_ATTEST.finalize_terminal(
                self.product, self.product, str(self.remote), "T-700", "attested-done"
            )

    def test_terminal_finalize_refuses_wrong_basis(self):
        def terminal(*_args):
            return {"basis": "attested-done", "ticket": "T-700"}

        def git(*_args, **_kwargs):
            return subprocess.CompletedProcess([], 0, "", "")

        with (
            patch.object(TICKET_ATTEST, "protected_terminal", side_effect=terminal),
            patch.object(TICKET_ATTEST, "git", side_effect=git),
            self.assertRaisesRegex(TICKET_ATTEST.Refusal, "wrong basis"),
        ):
            TICKET_ATTEST.finalize_terminal(
                self.product, self.product, str(self.remote), "T-700",
                "attested-emergency-closeout",
            )

    def test_terminal_controller_event_is_append_once(self):
        state = self.temp / "controller-events"
        state.mkdir(mode=0o700)
        state = state.resolve()
        terminal = {
            "basis": "attested-emergency-closeout",
            "protected_main": "b" * 40,
        }
        with patch.dict(os.environ, {"FACTORY_CONTROLLER_STATE_DIR": str(state)}):
            TICKET_ATTEST.record_terminal_controller_event(
                "T-700", KIT_SHA, terminal
            )
            TICKET_ATTEST.record_terminal_controller_event(
                "T-700", KIT_SHA, terminal
            )

        events = list((state / "events").glob("*.json"))
        self.assertEqual(len(events), 1)
        value = json.loads(events[0].read_text())
        digest = value.pop("event_sha256")
        encoded = json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ).encode()
        self.assertEqual(digest, hashlib.sha256(encoded).hexdigest())
        self.assertEqual(value["event"], "operator_terminal_recorded")
        self.assertEqual(value["terminal_basis"], "attested-emergency-closeout")
        self.assertEqual(value["protected_main"], "b" * 40)

    def test_overlay_consumption_uses_launcher_bound_operator_map(self):
        external = self.temp / "operator-map.json"
        operator = {
            "approval": "Receipt",
            "initiative": "I-1",
            "priority": "normal",
            "state": "Approved",
        }
        external.write_text(json.dumps({
            "tickets": {"T-700": {"operator": operator}},
        }))
        version = hashlib.sha256(json.dumps(
            operator, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        with patch.dict(os.environ, {"FACTORY_OPERATOR_MAP": str(external)}):
            TICKET_ATTEST.consume_overlay(self.product, "T-700", version)
        self.assertNotIn(
            "operator", json.loads(external.read_text())["tickets"]["T-700"]
        )
        self.assertFalse((self.product / "factory/operator-map.json").exists())

    def tearDown(self):
        shutil.rmtree(self.temp)

    @staticmethod
    def ticket(state):
        return f"""# T-700

State: {state}
Priority: normal
Merge-Policy: manual

## Factory checklist
- [x] Reviewer approved
- [ ] Evidence bundle posted
- [ ] Operator approved
- [ ] PR merged and staging confirmed

## Links
- PR:
- Evidence:
"""

    def commit(self, message):
        command("git", "add", ".", cwd=self.product)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", message, cwd=self.product,
        )

    def assert_receipt_only_refresh_replay(
        self, checklist, error=None, *, dependency_replay=False,
    ):
        ticket = self.product / "factory/tickets/T-700.md"
        text = self.ticket("Review")
        if dependency_replay:
            text = text.replace(
                "Priority: normal", "Priority: normal\nDepends-On: none",
            )
        start = text.index("- [ ] Evidence bundle posted")
        end = text.index("- [ ] PR merged and staging confirmed")
        ticket.write_text(
            text[:start] + checklist + text[end:] + "\nreviewer round 1: APPROVE\n",
            encoding="utf-8",
        )
        if command("git", "diff", "--quiet", cwd=self.product, check=False).returncode:
            self.commit("set historical refresh checklist")
            command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        old_head = self.head()
        updater = self.temp / "receipt-only-main"
        command("git", "clone", "-q", "--branch", "main", str(self.remote), str(updater))
        (updater / "main.txt").write_text("advance main\n", encoding="utf-8")
        command("git", "add", ".", cwd=updater)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "advance main", cwd=updater,
        )
        base_head = self.head_at(updater)
        command("git", "push", "-q", "origin", "main", cwd=updater)
        command("git", "fetch", "-q", "origin", "main", cwd=self.product)
        command("git", "merge", "-q", "--no-ff", "--no-edit", base_head, cwd=self.product)
        merge_head = self.head()
        receipt = self.product / "factory/attestations/T-700/refresh.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({
            "schema": "nysa.software-factory.ticket-refresh/v1",
            "ticket": "T-700", "generation": 1,
            "old_head": old_head, "base_head": base_head, "merge_head": merge_head,
            "prior_reviewer_runs": 1, "prior_approve_verdicts": 1,
            "prior_request_changes_verdicts": 0, "prior_narrator_runs": 1,
            "prior_bundle_blob": None, "prior_approval_blob": None,
            "refreshed_at": "2026-07-17T14:00:00Z",
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.commit("record receipt-only refresh")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        if error:
            with self.assertRaisesRegex(TICKET_ATTEST.Refusal, error):
                TICKET_ATTEST.publication_refresh_replay(
                    self.product, "T-700", "ticket/T-700", str(self.remote), base_head,
                )
            return
        if dependency_replay:
            with patch.object(TICKET_ATTEST, "exact_pr", return_value={
                "autoMergeRequest": None,
                "headRefOid": self.head(),
                "isDraft": True,
            }):
                result = TICKET_ATTEST.dependency_publication_replay(
                    argparse.Namespace(ticket="T-700"), self.product,
                    self.product, "ticket/", str(self.remote),
                )
            self.assertEqual(result["dependencies"], [])
            self.assertEqual(result["dependency_terminals"], [])
        else:
            result = TICKET_ATTEST.publication_refresh_replay(
                self.product, "T-700", "ticket/T-700", str(self.remote),
                base_head,
            )
        self.assertEqual(result["head"], self.head())

    def head(self):
        return command("git", "rev-parse", "HEAD", cwd=self.product).stdout.strip()

    @staticmethod
    def head_at(path):
        return command("git", "rev-parse", "HEAD", cwd=path).stdout.strip()

    def write_runs(self):
        fields = (
            "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,"
            "run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version\n"
        )
        rows = []
        plan_digest = hashlib.sha256(
            (self.product / "factory/route-plans/T-700.json").read_bytes()
        ).hexdigest()
        for index, role in enumerate(("reviewer", "narrator"), 1):
            run_id = f"{role}-1"
            (self.product / f"factory/runs/{run_id}.meta").write_text(
                f"run_id={run_id}\nphase=completed\naccounting_schema=1\n"
                "accounting_state=completed\nreserved_usd=1\ngo_issued=1\n"
                "task_submitted=1\nrole_exit=ok\nstarted_at=2026-07-17T12:00:00Z\n"
                "prompt_version=1\nturns=1\neffective_cost=0.1\ncost_basis=reported\n"
                f"exit_status=0\nticket=T-700\nrole={role}\nadapter=mock\n"
                "provider_family=anthropic\nselection_reason=pinned_route_plan\n"
                "adapter_version=1\nmodel_id=mock\neffort=medium\nroute_id=mock-route\n"
                "gateway_id=direct\ninference_provider_id=test-provider\n"
                "account_route_id=test-account\ntransport=test\n"
                f"policy_hash={'d' * 64}\nroute_plan_sha256={plan_digest}\nkit_sha={KIT_SHA}\n"
                f"role_head_before={self.reviewed}\nterminal_at=2026-07-17T12:0{index}:00Z\n"
            )
            rows.append(
                f"2026-07-17,12:0{index}:00,T-700,{role},mock,1,1,0.1,0,{run_id},"
                "anthropic,mock,pinned_route_plan,reported,1\n"
            )
        (self.product / "factory/runtime-ledger.csv").write_text(fields + "".join(rows))

    def write_narrator_passport(self, head, parent):
        module = TICKET_ATTEST.passport_module()
        secret = b"p" * 32
        (self.controller / "passport.key").write_bytes(secret)
        (self.controller / "passport.key").chmod(0o600)
        passports = self.controller / "passports"
        passports.mkdir(mode=0o700, exist_ok=True)
        completed = []
        for role, receipt in (("reviewer", "1" * 64), ("narrator", "2" * 64)):
            path = self.product / f"factory/runs/{role}-1.meta"
            raw = re.sub(
                r"(?m)^role_head_before=[0-9a-f]{40}$",
                f"role_head_before={parent if role == 'narrator' else self.reviewed}",
                path.read_text(),
            )
            raw += (
                f"contract_version=1.8.0\noutput_sha256={'3' * 64}\n"
                f"transition_receipt_sha256={receipt}\n"
            )
            path.write_text(raw)
            value = TICKET_ATTEST.meta(path)
            completed.append({
                "contract_version": "1.8.0",
                "factory_sha": KIT_SHA,
                "head_before": value["role_head_before"],
                "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "output_sha256": "3" * 64,
                "role": role,
                "run_id": f"{role}-1",
                "transition_receipt_sha256": receipt,
            })
        signed = module.authenticate({
            "branch": "ticket/T-700",
            "completed_role_evidence": completed,
            "contract_version": "1.8.0",
            "factory_release_history": [{
                "contract_version": "1.8.0", "factory_sha": KIT_SHA,
            }],
            "factory_sha": KIT_SHA,
            "head_sha": head,
            "migration_history": [],
            "project": "example-product",
            "schema": "nysa.software-factory.ticket-passport/v1",
            "ticket": "T-700",
        }, secret)
        path = passports / "T-700.json"
        path.write_bytes(module.canonical(signed))
        path.chmod(0o600)

    def add_legacy_planner(self):
        old_kit = "e" * 40
        run_id = "1700000000-100"
        legacy = self.product / f"factory/runs/{run_id}.meta"
        legacy.write_text(
            f"run_id={run_id}\nphase=completed\naccounting_schema=1\n"
            "accounting_state=completed\nreserved_usd=1\ngo_issued=1\n"
            "started_at=2026-07-17T10:57:00Z\nterminal_at=2026-07-17T10:58:00Z\n"
            "prompt_version=1\nturns=1\neffective_cost=0.1\nexit_status=0\n"
            "cost_basis=reported\nticket=T-700\nrole=planner\nadapter=mock\n"
            "provider_family=anthropic\nmodel_id=mock\neffort=medium\n"
            "selection_reason=primary_ready\nadapter_version=1\n"
            "primary_probe=READY:local_contract_ready\n"
            f"kit_sha={old_kit}\nkit_tree={'b' * 40}\nproduct_tree={'c' * 40}\n"
            f"ticket_kit_sha={old_kit}\ncontract_version=1.2.0\n"
            f"physical_kit_path=/factory/releases/{old_kit}\n"
            "kit_provenance_mode=sealed\npid=100\npgid=100\nprocess_start=test\n"
            "role_exit=ok\nrole_branch_before=ticket/T-700\n"
            f"role_head_before={self.reviewed}\nrole_remote_before={self.reviewed}\n"
            "updated_at=2026-07-17T10:58:00Z\n"
        )
        plan_digest = hashlib.sha256(
            (self.product / "factory/route-plans/T-700.json").read_bytes()
        ).hexdigest()
        pinned_id = "planner-pinned-1"
        (self.product / f"factory/runs/{pinned_id}.meta").write_text(
            f"run_id={pinned_id}\nphase=completed\naccounting_schema=1\n"
            "accounting_state=completed\ngo_issued=1\ntask_submitted=1\nrole_exit=ok\n"
            "cost_basis=reported\nexit_status=0\nticket=T-700\nrole=planner\nadapter=mock\n"
            "provider_family=anthropic\nmodel_id=mock\neffort=medium\n"
            "selection_reason=pinned_route_plan\nadapter_version=1\n"
            "route_id=mock-route\ngateway_id=direct\n"
            "inference_provider_id=test-provider\naccount_route_id=test-account\n"
            "transport=test\n"
            f"policy_hash={'d' * 64}\nroute_plan_sha256={plan_digest}\nkit_sha={KIT_SHA}\n"
            f"role_head_before={self.reviewed}\nterminal_at=2026-07-17T11:59:00Z\n"
        )
        ledger = self.product / "factory/runtime-ledger.csv"
        header, rows = ledger.read_text().split("\n", 1)
        legacy_row = (
            f"2026-07-17,10:58:00,T-700,planner,mock,1,1,0.1,0,{run_id},"
            "anthropic,mock,primary_ready,reported,1\n"
        )
        pinned_row = (
            f"2026-07-17,11:59:00,T-700,planner,mock,1,1,0.1,0,{pinned_id},"
            "anthropic,mock,pinned_route_plan,reported,1\n"
        )
        ledger.write_text(header + "\n" + legacy_row + pinned_row + rows)
        return legacy

    def write_state(self, **updates):
        value = {
            "duplicate": False, "wrong_head": False, "merge_fail": False,
            "auto_merge_confirm": True,
            "auto_merge": True, "draft": True, "merged": False, "merge_sha": "b" * 40,
            "merge_state": "BLOCKED",
            "merge_on_second_open": False, "open_list_count": 0,
            "pr_head": None, "checks": {"ci": True, "deploy-production": True},
            "check_runs": {},
            "closeout_pr": "absent", "closeout_duplicate": False,
            "closeout_wrong": False, "closeout_head": None,
            "closeout_merge_state": "BLOCKED",
            "create_fail": False, "closeout_merge_fail": False,
            "closeout_auto_merge": True,
            "historical_head_ref": None,
            "network_fail": False,
        }
        value.update(updates)
        self.state.write_text(json.dumps(value))

    def update_state(self, **updates):
        value = json.loads(self.state.read_text())
        value.update(updates)
        self.state.write_text(json.dumps(value))

    def install_fake_gh(self):
        path = self.bin / "gh"
        path.write_text("""#!/usr/bin/env python3
import json, os, subprocess, sys, urllib.parse
from pathlib import Path
s = json.loads(Path(os.environ["FAKE_GH_STATE"]).read_text())
a = sys.argv[1:]
if s.get("network_fail"):
    print("Could not resolve host: github.com", file=sys.stderr)
    raise SystemExit(1)
head = subprocess.check_output(["git", "-C", os.environ["FAKE_WORKDIR"], "rev-parse", "HEAD"], text=True).strip()
if a[:2] == ["pr", "list"]:
    state = a[a.index("--state") + 1]
    requested_head = a[a.index("--head") + 1]
    if requested_head.startswith("chore/"):
        if s["closeout_pr"] == "absent":
            print("[]")
        else:
            item = {"number": 14,
                    "headRefName": "chore/wrong-closeout" if s["closeout_wrong"] else requested_head,
                    "baseRefName": "develop" if s["closeout_wrong"] else "main",
                    "headRefOid": ("c" * 40 if s["closeout_wrong"] else (s.get("closeout_head") or head)),
                    "url": "https://example.invalid/pr/14",
                    "state": ("MERGED" if s["closeout_pr"] == "merged" else
                              "CLOSED" if s["closeout_pr"] == "closed" else "OPEN"),
                    "mergedAt": "2026-07-17T19:00:00Z" if s["closeout_pr"] == "merged" else None,
                    "mergeCommit": {"oid": "e" * 40} if s["closeout_pr"] == "merged" else None,
                    "mergeStateStatus": s["closeout_merge_state"]}
            print(json.dumps([item, dict(item, number=15)] if s["closeout_duplicate"] else [item]))
    else:
        if state == "open":
            s["open_list_count"] = s.get("open_list_count", 0) + 1
            Path(os.environ["FAKE_GH_STATE"]).write_text(json.dumps(s))
            if s.get("merge_on_second_open") and s["open_list_count"] >= 2:
                print("[]"); raise SystemExit(0)
        item = {"number": 7, "headRefName": "ticket/T-700", "baseRefName": "main",
                "headRefOid": ("c" * 40 if s["wrong_head"] else (s.get("pr_head") or head)), "url": "https://example.invalid/pr/7",
                "isDraft": s["draft"],
                "state": "MERGED" if state == "all" and s["merged"] else "OPEN",
                "mergedAt": "2026-07-17T18:00:00Z" if s["merged"] else None,
                "mergeCommit": {"oid": s["merge_sha"]} if s["merged"] else None}
        print(json.dumps([] if state == "open" and s["merged"] else
                         ([item, dict(item, number=8)] if s["duplicate"] else [item])))
elif a[:2] == ["pr", "create"]:
    if s["create_fail"]: print("create unavailable", file=sys.stderr); raise SystemExit(1)
    s["closeout_pr"] = "open"
    s["closeout_head"] = head
    s["create_argv"] = a
    s["create_count"] = s.get("create_count", 0) + 1
    Path(os.environ["FAKE_GH_STATE"]).write_text(json.dumps(s))
    print("https://example.invalid/pr/14")
elif a[:2] == ["pr", "merge"]:
    closeout = a[2] == "14"
    if not closeout and "--disable-auto" not in a and s["draft"]:
        print("draft pull request", file=sys.stderr); raise SystemExit(1)
    if (closeout and s["closeout_merge_fail"]) or (not closeout and s["merge_fail"]):
        print("auto-merge unavailable", file=sys.stderr); raise SystemExit(1)
    if closeout and "--disable-auto" in a:
        s["closeout_auto_merge"] = False
    elif not closeout and "--disable-auto" in a:
        s["auto_merge"] = False
    elif not closeout:
        s["auto_merge"] = s["auto_merge_confirm"]
    s["closeout_merge_argv" if closeout else "merge_argv"] = a
    Path(os.environ["FAKE_GH_STATE"]).write_text(json.dumps(s))
elif a[:2] == ["pr", "close"]:
    s["closeout_pr"] = "closed"
    s["closeout_merge_state"] = "BLOCKED"
    s["closeout_auto_merge"] = True
    s["closeout_close_argv"] = a
    Path(os.environ["FAKE_GH_STATE"]).write_text(json.dumps(s))
elif a[:2] == ["pr", "ready"]:
    s["draft"] = "--undo" in a
    Path(os.environ["FAKE_GH_STATE"]).write_text(json.dumps(s))
elif a[:2] == ["pr", "view"]:
    closeout = a[2] == "14"
    if closeout:
        print(json.dumps({"number": 14, "headRefName": "chore/t700-closeout",
                          "baseRefName": "main", "headRefOid": s.get("closeout_head") or head,
                          "state": ("MERGED" if s["closeout_pr"] == "merged" else
                                    "CLOSED" if s["closeout_pr"] == "closed" else "OPEN"),
                          "mergedAt": "2026-07-17T19:00:00Z" if s["closeout_pr"] == "merged" else None,
                          "mergeStateStatus": s["closeout_merge_state"],
                          "autoMergeRequest": {"mergeMethod": "SQUASH"} if s["closeout_auto_merge"] else None}))
    else:
        print(json.dumps({"number": 7,
                          "headRefName": s.get("historical_head_ref") or "ticket/T-700",
                          "baseRefName": "main",
                          "headRefOid": "c" * 40 if s["wrong_head"] else (s.get("pr_head") or head),
                          "state": "MERGED" if s["merged"] else "OPEN",
                          "mergedAt": "2026-07-17T18:00:00Z" if s["merged"] else None,
                          "mergeCommit": {"oid": s["merge_sha"]} if s["merged"] else None,
                          "mergeStateStatus": s["merge_state"],
                          "isDraft": s["draft"],
                          "autoMergeRequest": {"mergeMethod": "SQUASH"} if s["auto_merge"] else None}))
elif a[:1] == ["api"]:
    if a[1] == "repos/acme/factory/issues/269":
        print(json.dumps({"number": 269, "html_url": "https://github.com/acme/factory/issues/269", "state": "open"}))
    elif a[1].endswith("/status"):
        print(json.dumps({"statuses": [{"context": k, "state": "success" if v else "failure"}
                                      for k, v in s["checks"].items()]}))
    else:
        query = urllib.parse.urlparse(a[1]).query
        name = urllib.parse.parse_qs(query).get("check_name", [""])[0]
        print(json.dumps({"check_runs": s["check_runs"].get(name, [])}))
else:
    raise SystemExit(2)
""")
        path.chmod(0o755)

    def attest(self, action, *, attest_only=False):
        arguments = [
            sys.executable, str(SCRIPT), "--ticket", "T-700",
            "--workdir", str(self.workdir), "--action", action,
        ]
        if attest_only:
            arguments.append("--attest-only")
        return command(*arguments, env=self.env, check=False)

    def bundle(self):
        result = self.attest("bundle")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def prepare_post_review_evidence(self, *, unreferenced=False):
        bundle = self.product / "factory/tickets/T-700-bundle.md"
        evidence = self.product / "factory/tickets/T-700-evidence"
        evidence.mkdir()
        (evidence / "old.png").write_bytes(PNG)
        bundle.write_text(
            bundle.read_text().replace(
                "## Screenshots\nNo visual change.\n",
                "## Screenshots\n![Old](T-700-evidence/old.png)\n",
            )
        )
        self.commit("record prior narrator evidence")
        self.reviewed = self.head()
        self.write_runs()

        (evidence / "old.png").unlink()
        (evidence / "current.png").write_bytes(PNG)
        bundle.write_text(
            bundle.read_text().replace(
                "![Old](T-700-evidence/old.png)",
                "![Current](T-700-evidence/current.png)",
            )
        )
        if unreferenced:
            (evidence / "orphan.png").write_bytes(PNG)
        self.commit("refresh narrator evidence")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)

    def issue_approve_receipt(self, *, issued_at="2099-01-01T00:00:00Z", blob=None):
        if blob is None:
            blob = command(
                "git", "hash-object", "factory/attestations/T-700/bundle.json",
                cwd=self.product,
            ).stdout.strip()
        value = operator_receipt.issue(
            self.controller, "T-700", "approve",
            {"bundle_attestation_blob": blob},
        )
        # Deterministic freshness: rewrite issued_at and re-sign the digest so
        # the receipt is strictly newer than any real bundle attestation.
        immutable = {
            key: item for key, item in value.items()
            if key not in {"consumed", "consumed_at_epoch", "receipt_sha256"}
        }
        immutable["issued_at"] = issued_at
        immutable["receipt_sha256"] = hashlib.sha256(
            operator_receipt.canonical(immutable)
        ).hexdigest()
        immutable["consumed"] = False
        path = (
            self.controller / "operator-receipts" / "T-700"
            / f"approve-{value['sequence']}.json"
        )
        path.write_text(json.dumps(immutable, indent=2, sort_keys=True) + "\n")
        return immutable

    def write_operator_overlay(self, stamp, digest):
        (self.product / "factory/operator-map.json").write_text(json.dumps({
            "tickets": {"T-700": {"operator": {
                "state": "Approved", "approval": "Receipt",
                "state_base": "awaiting approval",
                "observed_at": stamp, "receipt_sha256": digest,
            }}},
        }))

    def approval_overlay(self, stale=False):
        if stale:
            # Refused on observed_at before any receipt lookup happens.
            self.write_operator_overlay("2020-01-01T00:00:00Z", "0" * 64)
            return
        stamp = "2099-01-01T00:00:00Z"
        receipt = self.issue_approve_receipt(issued_at=stamp)
        self.write_operator_overlay(stamp, receipt["receipt_sha256"])
        return receipt

    def project_approval_overlay(self):
        (self.product / "factory/operator-map.json").write_text(json.dumps({
            "tickets": {"T-700": {"operator": {
                "initiative": "I-1", "priority": "normal",
            }}},
        }))

    def append_successor_route(self):
        route = self.product / "factory/route-plans/T-700.json"
        value = json.loads(route.read_text())
        value["successor_test"] = True
        route.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        self.commit("migrate approved route")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)

    def test_bundle_and_approval_happy_path_and_retry(self):
        self.bundle()
        self.approval_overlay()
        result = self.attest("approval")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("State: Approved", (self.product / "factory/tickets/T-700.md").read_text())
        state = json.loads(self.state.read_text())
        self.assertFalse(state["draft"])
        self.assertIn("--squash", state["merge_argv"])
        self.assertNotIn("--merge", state["merge_argv"])

    def test_bundle_recovers_confirmed_push_after_tracking_update_loss(self):
        self.env["FACTORY_TEST_REFRESH_CRASH_AFTER_PUSH"] = "1"
        crashed = self.attest("bundle")
        self.env.pop("FACTORY_TEST_REFRESH_CRASH_AFTER_PUSH")
        self.assertEqual(crashed.returncode, 92, crashed.stderr)
        attested = self.head()
        self.assertEqual(
            command(
                "git", "ls-remote", "--heads", str(self.remote),
                "refs/heads/ticket/T-700",
            ).stdout.split()[0],
            attested,
        )

        retried = self.attest("bundle")

        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(self.head(), attested)
        self.assertEqual(json.loads(retried.stdout)["action"], "bundle")

    def test_bundle_network_outage_waits_without_mutation(self):
        before = self.head()
        self.update_state(network_fail=True)

        waiting = self.attest("bundle")

        self.assertEqual(waiting.returncode, 75, waiting.stderr)
        self.assertEqual(json.loads(waiting.stdout), {
            "reason_code": "external_unavailable", "status": "wait",
        })
        self.assertEqual(self.head(), before)
        self.update_state(network_fail=False)
        self.assertEqual(self.attest("bundle").returncode, 0)

    def test_remote_timeout_is_typed_without_classifying_local_timeout(self):
        timeout = subprocess.TimeoutExpired(["gh", "pr", "view"], 120)
        with patch.object(TICKET_ATTEST.subprocess, "run", side_effect=timeout):
            with self.assertRaises(TICKET_ATTEST.ExternalUnavailable):
                TICKET_ATTEST.run(["gh", "pr", "view"])
            with self.assertRaises(subprocess.TimeoutExpired):
                TICKET_ATTEST.run(["git", "status"])

    def test_push_accepts_an_exact_head_after_lost_response(self):
        head = "b" * 40
        results = [
            subprocess.CompletedProcess([], 0, str(self.remote) + "\n", ""),
            subprocess.CompletedProcess(
                [], 128, "", "connection reset by peer",
            ),
            subprocess.CompletedProcess(
                [], 0, f"{head}\trefs/heads/ticket/T-700\n", "",
            ),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        with patch.object(TICKET_ATTEST.subprocess, "run", side_effect=results):
            self.assertEqual(
                TICKET_ATTEST.push_head(
                    self.product, self.product, str(self.remote),
                    "ticket/T-700", head,
                ),
                head,
            )

    def test_bundle_retries_a_locally_committed_rejected_push(self):
        hook = self.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        failed = self.attest("bundle")
        committed = self.head()
        hook.unlink()

        retried = self.attest("bundle")

        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(self.head(), committed)

    def test_bundle_retry_refuses_an_unrelated_later_head(self):
        self.bundle()
        (self.product / "unrelated.txt").write_text("later\n")
        self.commit("unrelated post-bundle commit")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)

        retried = self.attest("bundle")

        self.assertNotEqual(retried.returncode, 0)
        self.assertIn("bundle attestation commit", retried.stderr)

    def test_approval_refuses_cleanly_when_operator_map_is_absent(self):
        self.bundle()
        result = self.attest("approval")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exact operator", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_bundle_accepts_completed_conservative_cursor_accounting(self):
        manifest = self.product / "factory/runs/reviewer-1.meta"
        manifest.write_text(
            manifest.read_text()
            .replace("accounting_state=completed", "accounting_state=abandoned_conservative")
            .replace("cost_basis=reported", "cost_basis=conservative_reservation")
        )
        ledger = self.product / "factory/runtime-ledger.csv"
        ledger.write_text(ledger.read_text().replace(
            "reviewer-1,anthropic,mock,pinned_route_plan,reported,1",
            "reviewer-1,anthropic,mock,pinned_route_plan,conservative_reservation,1",
        ))
        self.bundle()

    def test_bundle_accepts_referenced_current_ticket_png_evidence(self):
        self.prepare_post_review_evidence()
        self.bundle()

    def test_bundle_retains_authenticated_prior_narrator_png(self):
        bundle = self.product / "factory/tickets/T-700-bundle.md"
        evidence = self.product / "factory/tickets/T-700-evidence"
        evidence.mkdir()
        (evidence / "retained.png").write_bytes(PNG)
        bundle.write_text(bundle.read_text().replace(
            "## Screenshots\nNo visual change.\n",
            "## Screenshots\n![Retained](T-700-evidence/retained.png)\n",
        ))
        self.commit("record prior narrator bundle")
        parent = self.head()
        (evidence / "current.png").write_bytes(PNG)
        bundle.write_text(bundle.read_text().replace(
            "![Retained](T-700-evidence/retained.png)",
            "![Current](T-700-evidence/current.png)",
        ))
        self.commit("replace narrator bundle")
        ticket = self.product / "factory/tickets/T-700.md"
        ticket.write_text(ticket.read_text() + "Publication evidence refreshed.\n")
        self.commit("record later factory metadata")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        head = self.head()

        result = self.attest("bundle")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("changed after", result.stderr)

        self.write_narrator_passport(head, parent)
        self.bundle()

    def test_bundle_counts_only_current_qualification_review_evidence(self):
        baseline = self.head()
        ticket = self.product / "factory/tickets/T-700.md"
        ticket.write_text(ticket.read_text() + "reviewer round 2: APPROVE\n")
        self.commit("record current qualification review")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.env.update({
            "FACTORY_KIT_TRUST_SCOPE": "qualification-candidate",
            "FACTORY_QUALIFICATION_PRODUCT_SHA": baseline,
        })

        self.bundle()

    def test_bundle_refuses_unreferenced_narrator_evidence(self):
        self.prepare_post_review_evidence(unreferenced=True)
        result = self.attest("bundle")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("changed after", result.stderr)

    def test_bundle_refuses_changed_merge_policy(self):
        ticket = self.product / "factory/tickets/T-700.md"
        ticket.write_text(ticket.read_text().replace(
            "Merge-Policy: manual", "Merge-Policy: auto",
        ))
        self.commit("grant branch auto merge")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        result = self.attest("bundle")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Merge-Policy differs from protected origin/main", result.stderr)

    def test_stale_approval_is_refused(self):
        self.bundle()
        self.approval_overlay(stale=True)
        self.assertIn("not newer", self.attest("approval").stderr)

    def test_approval_without_state_dir_receipt_is_refused(self):
        self.bundle()
        self.write_operator_overlay("2099-01-01T00:00:00Z", "1" * 64)
        result = self.attest("approval")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stale_operator_approval", result.stderr)
        self.assertIn("no unconsumed operator approval", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_approval_refuses_receipt_bound_to_other_attestation(self):
        self.bundle()
        receipt = self.issue_approve_receipt(blob="0" * 40)
        self.write_operator_overlay(
            "2099-01-01T00:00:00Z", receipt["receipt_sha256"],
        )
        result = self.attest("approval")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no unconsumed operator approval", result.stderr)

    def test_approval_consumes_map_bound_receipt_not_newest_open_receipt(self):
        self.bundle()
        selected = self.issue_approve_receipt()
        newest = self.issue_approve_receipt(blob="0" * 40)
        self.write_operator_overlay(
            "2099-01-01T00:00:00Z", selected["receipt_sha256"],
        )

        result = self.attest("approval")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(operator_receipt.read_exact(
            self.controller, "T-700", "approve", selected["receipt_sha256"],
        )["consumed"])
        self.assertFalse(operator_receipt.read_exact(
            self.controller, "T-700", "approve", newest["receipt_sha256"],
        )["consumed"])

    def test_approval_refuses_already_consumed_receipt(self):
        self.bundle()
        receipt = self.issue_approve_receipt()
        operator_receipt.verify_consume(self.controller, "T-700", "approve")
        self.write_operator_overlay(
            "2099-01-01T00:00:00Z", receipt["receipt_sha256"],
        )
        result = self.attest("approval")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no unconsumed operator approval", result.stderr)

    def test_second_approval_after_consumption_clears_replayed_overlay(self):
        self.bundle()
        self.approval_overlay()
        self.assertEqual(self.attest("approval").returncode, 0)
        approval = self.product / "factory/attestations/T-700/approval.json"
        digest = json.loads(approval.read_text())["receipt_sha256"]
        self.write_operator_overlay("2099-01-01T00:00:00Z", digest)
        result = self.attest("approval")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(
            "operator",
            json.loads(
                (self.product / "factory/operator-map.json").read_text()
            )["tickets"]["T-700"],
        )

    def test_historical_linear_approval_validates_through_continuation(self):
        self.bundle()
        self.approval_overlay()
        phase_one = self.attest("approval", attest_only=True)
        self.assertEqual(phase_one.returncode, 0, phase_one.stderr)
        approval = self.product / "factory/attestations/T-700/approval.json"
        value = json.loads(approval.read_text())
        value.pop("receipt_sha256")
        value["linear_updated_at"] = value["observed_at"]
        approval.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        ticket = self.product / "factory/tickets/T-700.md"
        ticket.write_text(ticket.read_text().replace(
            "Operator-Approval: Receipt", "Operator-Approval: Linear",
        ))
        command("git", "add", "-A", cwd=self.product)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "--amend", "-qm", "T-700: attest operator approval",
            cwd=self.product,
        )
        command(
            "git", "push", "-q", "--force", "origin", "ticket/T-700",
            cwd=self.product,
        )
        self.project_approval_overlay()
        result = self.attest("approval")
        self.assertEqual(result.returncode, 0, result.stderr)
        second = json.loads(result.stdout)
        self.assertEqual(second["action"], "approval")
        self.assertTrue(second["auto_merge"])
        self.assertIn(
            "Operator-Approval: Linear",
            (self.product / "factory/tickets/T-700.md").read_text(),
        )

    def test_merge_method_is_required_and_allowlisted(self):
        project = self.product / "factory/PROJECT.env"
        project.write_text(project.read_text().replace(
            "AUTO_MERGE_METHOD=squash\n", "AUTO_MERGE_METHOD=octopus\n",
        ))
        self.assertIn("AUTO_MERGE_METHOD", self.attest("bundle").stderr)

    def test_changed_code_after_review_is_refused(self):
        (self.product / "app.txt").write_text("unreviewed\n")
        self.commit("unreviewed")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.assertIn("changed after", self.attest("bundle").stderr)

    def test_bundle_accepts_only_exact_post_review_migration_kit_pin(self):
        candidate = "c" * 40
        path = self.product / "factory/route-plans/T-700.json"
        legacy_raw = path.read_bytes()
        legacy = json.loads(legacy_raw)
        body = {
            "historical_selections": legacy["resolution"]["selections"],
            "kind": "migration",
            "legacy_plan_b64": base64.b64encode(legacy_raw).decode(),
            "legacy_plan_sha256": hashlib.sha256(legacy_raw).hexdigest(),
            "migrated_at": "2026-08-20T11:00:00Z",
            "new_kit_sha": candidate,
            "old_kit_sha": KIT_SHA,
            "pin_commit": self.head(),
            "policy_hash": legacy["resolution"]["policy_hash"],
        }
        revision = {"body": body, "parent_hash": None, "revision": 0}
        revision["revision_hash"] = TICKET_ATTEST.route_revision_hash(0, None, body)
        path.write_text(json.dumps({
            "kit_sha": candidate,
            "revisions": [revision],
            "schema": "ticket-model-route-journal/v2",
            "ticket": "T-700",
        }, sort_keys=True, separators=(",", ":")) + "\n")
        pin = self.product / "factory/KIT_PIN"
        pin.write_text("d" * 40 + "\n")
        self.commit("migrate with wrong Kit SHA")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.env["FACTORY_RELEASE_SHA"] = candidate
        self.assertIn(
            "post-review route migration Kit-SHA is invalid",
            self.attest("bundle").stderr,
        )

        pin.write_text(candidate + "\n")
        pin.chmod(0o755)
        self.commit("make successor pin executable")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.assertIn(
            "post-review route migration Kit-SHA is invalid",
            self.attest("bundle").stderr,
        )

        pin.chmod(0o644)
        self.commit("pin exact successor kit")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.bundle()

    def test_bundle_refuses_run_provenance_outside_pinned_route(self):
        manifest = self.product / "factory/runs/reviewer-1.meta"
        manifest.write_text(
            manifest.read_text().replace("route_id=mock-route", "route_id=other-route")
        )
        self.assertIn("does not match its pinned route", self.attest("bundle").stderr)

    def test_route_journal_binds_runs_to_historical_release_migrations(self):
        legacy = json.loads(
            (self.product / "factory/route-plans/T-700.json").read_text()
        )
        legacy["kit_sha"] = "b" * 40
        legacy_raw = (json.dumps(legacy, indent=2, sort_keys=True) + "\n").encode()
        resolution = legacy["resolution"]
        migration = {
            "historical_selections": resolution["selections"],
            "kind": "migration",
            "legacy_plan_b64": base64.b64encode(legacy_raw).decode(),
            "legacy_plan_sha256": hashlib.sha256(legacy_raw).hexdigest(),
            "migrated_at": "2026-07-17T11:10:00Z",
            "new_kit_sha": "b" * 40,
            "old_kit_sha": "b" * 40,
            "pin_commit": "1" * 40,
            "policy_hash": resolution["policy_hash"],
        }
        revision_zero = {
            "body": migration, "parent_hash": None, "revision": 0,
        }
        revision_zero["revision_hash"] = TICKET_ATTEST.route_revision_hash(
            0, None, migration
        )
        release = {
            "kind": "release-migration",
            "migrated_at": "2026-07-17T11:20:00Z",
            "new_kit_sha": KIT_SHA,
            "old_kit_sha": "b" * 40,
            "pin_commit": "2" * 40,
            "prior_resolution": resolution,
        }
        revision_one = {
            "body": release,
            "parent_hash": revision_zero["revision_hash"],
            "revision": 1,
        }
        revision_one["revision_hash"] = TICKET_ATTEST.route_revision_hash(
            1, revision_zero["revision_hash"], release
        )
        journal = {
            "kit_sha": KIT_SHA,
            "revisions": [revision_zero, revision_one],
            "schema": "ticket-model-route-journal/v2",
            "ticket": "T-700",
        }
        path = self.product / "factory/route-plans/T-700.json"
        path.write_text(json.dumps(journal, sort_keys=True, separators=(",", ":")) + "\n")

        def manifest(role, number, expected_kit, revisions):
            prefix = dict(journal)
            prefix["kit_sha"] = expected_kit
            prefix["revisions"] = journal["revisions"][:revisions]
            raw = (json.dumps(prefix, sort_keys=True, separators=(",", ":")) + "\n").encode()
            selection = resolution["selections"][role]
            return {
                "role": role,
                "selection_reason": "route_journal",
                "route_revision": str(number),
                "route_revision_hash": journal["revisions"][number]["revision_hash"],
                "route_plan_sha256": hashlib.sha256(raw).hexdigest(),
                "kit_sha": expected_kit,
                "policy_hash": resolution["policy_hash"],
                **{
                    field: selection[selected]
                    for field, selected in {
                        "adapter": "adapter", "provider_family": "provider_family",
                        "model_id": "selection_id", "effort": "effort",
                        "adapter_version": "adapter_version", "route_id": "route_id",
                        "gateway_id": "gateway_id",
                        "inference_provider_id": "inference_provider_id",
                        "account_route_id": "account_route_id", "transport": "transport",
                    }.items()
                },
            }

        evidence = TICKET_ATTEST.route_plan_evidence(
            self.product, self.product, "T-700", KIT_SHA,
            [manifest("reviewer", 0, "b" * 40, 1), manifest("narrator", 1, KIT_SHA, 2)],
        )
        self.assertEqual(
            evidence["route_plan_sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
        )
        self.assertEqual(evidence["policy_hash"], resolution["policy_hash"])
        self.assertNotIn("legacy_planner_manifest_sha256", evidence)

        release["prior_resolution_sha256"] = TICKET_ATTEST.content_hash(
            release.pop("prior_resolution")
        )
        revision_one["revision_hash"] = TICKET_ATTEST.route_revision_hash(
            1, revision_zero["revision_hash"], release
        )
        path.write_text(json.dumps(journal, sort_keys=True, separators=(",", ":")) + "\n")
        TICKET_ATTEST.route_plan_evidence(
            self.product, self.product, "T-700", KIT_SHA, []
        )

        release["prior_resolution_sha256"] = "0" * 64
        revision_one["revision_hash"] = TICKET_ATTEST.route_revision_hash(
            1, revision_zero["revision_hash"], release
        )
        path.write_text(json.dumps(journal, sort_keys=True, separators=(",", ":")) + "\n")
        with self.assertRaisesRegex(TICKET_ATTEST.Refusal, "extend the prior route"):
            TICKET_ATTEST.route_plan_evidence(
                self.product, self.product, "T-700", KIT_SHA, []
            )

        release["prior_resolution_sha256"] = TICKET_ATTEST.content_hash(resolution)
        release["new_resolution"] = json.loads(json.dumps(resolution))
        release["new_resolution"]["selections"]["narrator"]["route_id"] = "changed"
        revision_one["revision_hash"] = TICKET_ATTEST.route_revision_hash(
            1, revision_zero["revision_hash"], release
        )
        path.write_text(json.dumps(journal, sort_keys=True, separators=(",", ":")) + "\n")
        with self.assertRaisesRegex(TICKET_ATTEST.Refusal, "changed logical routing"):
            TICKET_ATTEST.route_plan_evidence(
                self.product, self.product, "T-700", KIT_SHA, []
            )

    def test_bundle_accepts_one_superseded_legacy_planner_and_binds_digest(self):
        legacy = self.add_legacy_planner()
        expected = hashlib.sha256(legacy.read_bytes()).hexdigest()

        self.bundle()

        receipt = json.loads(
            (self.product / "factory/attestations/T-700/bundle.json").read_text()
        )
        self.assertEqual(receipt["schema"], "nysa.software-factory.ticket-bundle/v2")
        self.assertEqual(receipt["legacy_planner_manifest_sha256"], expected)

    def test_bundle_refuses_primary_ready_for_a_non_planner(self):
        legacy = self.add_legacy_planner()
        legacy.write_text(legacy.read_text().replace("role=planner", "role=builder"))
        ledger = self.product / "factory/runtime-ledger.csv"
        ledger.write_text(ledger.read_text().replace(
            "T-700,planner,mock,1,1,0.1,0,1700000000-100,",
            "T-700,builder,mock,1,1,0.1,0,1700000000-100,",
        ))

        self.assertIn("must be a Planner", self.attest("bundle").stderr)

    def test_bundle_refuses_legacy_planner_without_pinned_supersession(self):
        self.add_legacy_planner()
        (self.product / "factory/runs/planner-pinned-1.meta").unlink()
        ledger = self.product / "factory/runtime-ledger.csv"
        ledger.write_text("\n".join(
            line for line in ledger.read_text().splitlines()
            if ",planner-pinned-1," not in line
        ) + "\n")

        self.assertIn("later pinned Planner", self.attest("bundle").stderr)

    def test_bundle_refuses_multiple_legacy_planners(self):
        legacy = self.add_legacy_planner()
        second = legacy.with_name("1700000000-101.meta")
        second.write_text(legacy.read_text().replace(
            "run_id=1700000000-100", "run_id=1700000000-101",
        ))
        ledger = self.product / "factory/runtime-ledger.csv"
        ledger.write_text(ledger.read_text() + (
            "2026-07-17,11:58:00,T-700,planner,mock,1,1,0.1,0,1700000000-101,"
            "anthropic,mock,primary_ready,reported,1\n"
        ))

        self.assertIn("exactly one", self.attest("bundle").stderr)

    def test_bundle_refuses_legacy_planner_route_mismatch(self):
        legacy = self.add_legacy_planner()
        legacy.write_text(legacy.read_text().replace("model_id=mock", "model_id=other"))
        ledger = self.product / "factory/runtime-ledger.csv"
        ledger.write_text(ledger.read_text().replace(
            "1700000000-100,anthropic,mock,primary_ready,",
            "1700000000-100,anthropic,other,primary_ready,",
        ))

        self.assertIn("does not match the pinned Planner route", self.attest("bundle").stderr)

    def test_later_request_changes_overrides_earlier_approve(self):
        ticket = self.product / "factory/tickets/T-700.md"
        ticket.write_text(ticket.read_text() + "reviewer round 2: REQUEST CHANGES — regression\n")
        ledger = self.product / "factory/runtime-ledger.csv"
        rows = ledger.read_text()
        plan_digest = hashlib.sha256(
            (self.product / "factory/route-plans/T-700.json").read_bytes()
        ).hexdigest()
        for index, role in ((3, "reviewer"), (4, "narrator")):
            run_id = f"{role}-2"
            (self.product / f"factory/runs/{run_id}.meta").write_text(
                f"run_id={run_id}\nphase=completed\naccounting_schema=1\n"
                "accounting_state=completed\ngo_issued=1\ntask_submitted=1\nrole_exit=ok\n"
                "cost_basis=reported\nexit_status=0\nticket=T-700\n"
                f"role={role}\nrole_head_before={self.reviewed}\n"
                "adapter=mock\nprovider_family=anthropic\nmodel_id=mock\neffort=medium\n"
                "selection_reason=pinned_route_plan\nadapter_version=1\n"
                "route_id=mock-route\ngateway_id=direct\n"
                "inference_provider_id=test-provider\naccount_route_id=test-account\n"
                "transport=test\n"
                f"policy_hash={'d' * 64}\nroute_plan_sha256={plan_digest}\nkit_sha={KIT_SHA}\n"
                f"terminal_at=2026-07-17T12:0{index}:00Z\n"
            )
            rows += (
                f"2026-07-17,12:0{index}:00,T-700,{role},mock,1,1,0.1,0,{run_id},"
                "anthropic,mock,pinned_route_plan,reported,1\n"
            )
        ledger.write_text(rows)
        self.commit("later rejection")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.assertIn("latest non-void", self.attest("bundle").stderr)

    def test_changed_bundle_after_attestation_is_refused(self):
        self.bundle()
        path = self.product / "factory/tickets/T-700-bundle.md"
        path.write_text(path.read_text() + "changed\n")
        self.commit("tamper bundle")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.approval_overlay()
        self.assertIn("bundle changed", self.attest("approval").stderr)

    def test_explicitly_non_approvable_bundle_is_refused(self):
        path = self.product / "factory/tickets/T-700-bundle.md"
        path.write_text("NOT APPROVABLE: preview is missing.\n" + path.read_text())
        self.commit("record failed bundle")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        result = self.attest("bundle")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("explicitly not approvable", result.stderr)

    def test_wrong_duplicate_and_head_mismatched_prs_are_refused(self):
        for update, message in (
            ({"duplicate": True}, "exactly one"),
            ({"wrong_head": True}, "head"),
        ):
            with self.subTest(update=update):
                self.write_state(**update)
                self.assertIn(message, self.attest("bundle").stderr)

    def test_auto_merge_failure_retains_overlay_and_can_retry(self):
        self.bundle()
        self.approval_overlay()
        self.write_state(merge_fail=True)
        self.assertIn("auto-merge", self.attest("approval").stderr)
        mapping = json.loads((self.product / "factory/operator-map.json").read_text())
        self.assertIn("operator", mapping["tickets"]["T-700"])
        self.write_state()
        self.assertEqual(self.attest("approval").returncode, 0)

    def test_approval_attestation_commits_h2_before_auto_merge(self):
        self.bundle()
        self.approval_overlay()
        reviewed_head = self.head()
        phase_one = self.attest("approval", attest_only=True)
        self.assertEqual(phase_one.returncode, 0, phase_one.stderr)
        attested_head = self.head()
        self.assertNotEqual(attested_head, reviewed_head)
        first = json.loads(phase_one.stdout)
        self.assertEqual(first["action"], "approval-attested")
        self.assertEqual(first["head"], attested_head)
        self.assertFalse(first["auto_merge"])
        self.assertFalse(json.loads(self.state.read_text())["auto_merge"])

        phase_two = self.attest("approval")
        self.assertEqual(phase_two.returncode, 0, phase_two.stderr)
        second = json.loads(phase_two.stdout)
        self.assertEqual(second["action"], "approval")
        self.assertEqual(second["head"], attested_head)
        self.assertTrue(second["auto_merge"])
        self.assertTrue(json.loads(self.state.read_text())["auto_merge"])

    def test_approval_recovers_confirmed_push_after_tracking_update_loss(self):
        self.bundle()
        self.approval_overlay()
        self.env["FACTORY_TEST_REFRESH_CRASH_AFTER_PUSH"] = "1"
        crashed = self.attest("approval", attest_only=True)
        self.env.pop("FACTORY_TEST_REFRESH_CRASH_AFTER_PUSH")
        self.assertEqual(crashed.returncode, 92, crashed.stderr)
        attested = self.head()

        retried = self.attest("approval", attest_only=True)

        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(self.head(), attested)
        self.assertEqual(
            json.loads(retried.stdout)["action"], "approval-attested",
        )

    def test_approval_retries_a_locally_committed_rejected_push(self):
        self.bundle()
        self.approval_overlay()
        hook = self.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        failed = self.attest("approval", attest_only=True)
        committed = self.head()
        hook.unlink()

        retried = self.attest("approval", attest_only=True)

        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(self.head(), committed)

    def test_projected_overlay_retries_approval_after_successor_route(self):
        self.bundle()
        self.approval_overlay()
        phase_one = self.attest("approval", attest_only=True)
        self.assertEqual(phase_one.returncode, 0, phase_one.stderr)
        self.project_approval_overlay()
        self.append_successor_route()
        self.write_state(auto_merge=False)

        phase_two = self.attest("approval")

        self.assertEqual(phase_two.returncode, 0, phase_two.stderr)
        result = json.loads(phase_two.stdout)
        self.assertEqual(result["action"], "approval")
        self.assertEqual(result["head"], self.head())
        self.assertTrue(result["auto_merge"])
        operator = json.loads(
            (self.product / "factory/operator-map.json").read_text()
        )["tickets"]["T-700"]["operator"]
        self.assertEqual(operator, {"initiative": "I-1", "priority": "normal"})

    def test_partial_projected_approval_overlay_is_refused(self):
        self.bundle()
        self.approval_overlay()
        phase_one = self.attest("approval", attest_only=True)
        self.assertEqual(phase_one.returncode, 0, phase_one.stderr)
        mapping = json.loads(
            (self.product / "factory/operator-map.json").read_text()
        )
        mapping["tickets"]["T-700"]["operator"].pop("approval")
        (self.product / "factory/operator-map.json").write_text(
            json.dumps(mapping)
        )

        refused = self.attest("approval")

        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("exact operator", refused.stderr)

    def test_approval_retry_rejects_tampered_receipt_or_later_head(self):
        self.bundle()
        self.approval_overlay()
        self.write_state(merge_fail=True)
        self.assertNotEqual(self.attest("approval").returncode, 0)
        approval = self.product / "factory/attestations/T-700/approval.json"
        value = json.loads(approval.read_text())
        value["reviewed_sha"] = "d" * 40
        approval.write_text(json.dumps(value, sort_keys=True) + "\n")
        self.commit("tamper approval receipt")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.write_state()
        self.assertIn("invalid", self.attest("approval").stderr)

    def test_approval_retry_rejects_unrelated_later_head(self):
        self.bundle()
        self.approval_overlay()
        self.write_state(merge_fail=True)
        self.assertNotEqual(self.attest("approval").returncode, 0)
        (self.product / "unrelated.txt").write_text("later\n")
        self.commit("later unrelated head")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.write_state()
        self.assertIn("approval continuation", self.attest("approval").stderr)

    def test_auto_merge_unconfirmed_is_refused(self):
        self.bundle()
        self.approval_overlay()
        self.write_state(auto_merge=False, auto_merge_confirm=False)
        self.assertIn("did not confirm", self.attest("approval").stderr)

    def test_refresh_retires_stale_approval_and_binds_exact_main_merge(self):
        baseline = self.head()
        ticket_path = self.product / "factory/tickets/T-700.md"
        ticket_path.write_text(
            ticket_path.read_text() + "reviewer round 2: APPROVE\n"
        )
        self.commit("record current qualification review")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.env.update({
            "FACTORY_KIT_TRUST_SCOPE": "qualification-candidate",
            "FACTORY_QUALIFICATION_PRODUCT_SHA": baseline,
        })
        self.bundle()
        legacy_receipt = self.approval_overlay()
        self.assertEqual(self.attest("approval").returncode, 0)
        audit = {
            key: value for key, value in legacy_receipt.items()
            if key != "nonce"
        }
        audit["audit"] = "no-authority"
        audit_path = self.product / "factory/receipts/T-700/approve-1.json"
        audit_path.parent.mkdir(parents=True)
        audit_path.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n")
        self.commit("record legacy approval audit")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.approval_overlay()
        mapping = json.loads((self.product / "factory/operator-map.json").read_text())
        mapping["tickets"]["T-700"]["operator"]["priority"] = "urgent"
        (self.product / "factory/operator-map.json").write_text(json.dumps(mapping))
        old_head = self.head()
        approval = self.product / "factory/attestations/T-700/approval.json"
        old_approval_blob = command(
            "git", "hash-object", str(approval), cwd=self.product,
        ).stdout.strip()
        updater = self.temp / "main-update"
        command("git", "clone", "-q", "--branch", "main", str(self.remote), str(updater))
        (updater / "main.txt").write_text("protected update\n")
        main_ticket = updater / "factory/tickets/T-700.md"
        main_ticket.write_text(main_ticket.read_text().replace(
            "Priority: normal\n", "Priority: normal\nProtected-main note.\n",
        ))
        command("git", "add", ".", cwd=updater)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "advance protected main", cwd=updater,
        )
        command("git", "push", "-q", "origin", "main", cwd=updater)
        base_head = self.head_at(updater)
        self.update_state(merge_state="UNKNOWN")

        result = self.attest("refresh")
        self.assertEqual(result.returncode, 0, result.stderr)
        ticket = (self.product / "factory/tickets/T-700.md").read_text()
        self.assertIn("State: Review", ticket)
        self.assertIn("- [ ] Evidence bundle posted", ticket)
        self.assertIn("- [ ] Operator approved", ticket)
        self.assertIn("Protected-main note.", ticket)
        self.assertFalse(approval.exists())
        self.assertFalse((approval.parent / "bundle.json").exists())
        receipt = json.loads((approval.parent / "refresh.json").read_text())
        self.assertEqual(
            receipt["schema"], "nysa.software-factory.ticket-refresh/v2"
        )
        self.assertEqual(receipt["revalidation_factory_sha"], KIT_SHA)
        self.assertEqual(receipt["revalidation_generation"], 1)
        self.assertEqual(receipt["revalidation_budget_micro_usd"], 20_000_000)
        self.assertEqual(receipt["old_head"], old_head)
        self.assertEqual(receipt["base_head"], base_head)
        self.assertEqual(receipt["prior_approval_blob"], old_approval_blob)
        self.assertEqual(receipt["prior_reviewer_runs"], 1)
        self.assertEqual(receipt["prior_approve_verdicts"], 1)
        parents = command(
            "git", "rev-list", "--parents", "-n", "1", receipt["merge_head"],
            cwd=self.product,
        ).stdout.split()
        self.assertEqual(parents, [receipt["merge_head"], old_head, base_head])
        self.assertFalse(json.loads(self.state.read_text())["auto_merge"])
        operator = json.loads((self.product / "factory/operator-map.json").read_text())["tickets"]["T-700"]["operator"]
        self.assertEqual(operator, {"priority": "urgent"})
        self.assertIn("post-refresh Reviewer", self.attest("bundle").stderr)
        refreshed = self.head()
        ticket_path.write_text(
            ticket_path.read_text() + "\nOPERATOR NOTE: reviewer run 1 void — duplicate\n"
        )
        self.commit("try to remap stale reviewer verdict")
        void_head = self.head()
        plan_digest = hashlib.sha256(
            (self.product / "factory/route-plans/T-700.json").read_bytes()
        ).hexdigest()
        ledger = self.product / "factory/runtime-ledger.csv"
        rows = ledger.read_text()
        for index, role, role_head in (
            (3, "reviewer", refreshed),
            (4, "narrator", void_head),
        ):
            run_id = f"{role}-2"
            (self.product / f"factory/runs/{run_id}.meta").write_text(
                f"run_id={run_id}\nphase=completed\naccounting_schema=1\n"
                "accounting_state=completed\ngo_issued=1\ntask_submitted=1\nrole_exit=ok\n"
                "cost_basis=reported\nexit_status=0\nticket=T-700\n"
                f"role={role}\nrole_head_before={role_head}\n"
                "adapter=mock\nprovider_family=anthropic\nmodel_id=mock\neffort=medium\n"
                "selection_reason=pinned_route_plan\nadapter_version=1\n"
                "route_id=mock-route\ngateway_id=direct\n"
                "inference_provider_id=test-provider\naccount_route_id=test-account\n"
                "transport=test\n"
                f"policy_hash={'d' * 64}\nroute_plan_sha256={plan_digest}\nkit_sha={KIT_SHA}\n"
                f"terminal_at=2026-07-17T13:0{index}:00Z\n"
            )
            rows += (
                f"2026-07-17,13:0{index}:00,T-700,{role},mock,1,1,0.1,0,{run_id},"
                "anthropic,mock,pinned_route_plan,reported,1\n"
            )
        ledger.write_text(rows)
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.assertIn("new post-refresh Reviewer verdict", self.attest("bundle").stderr)
        ticket_path.write_text(
            ticket_path.read_text().replace(
                "\nOPERATOR NOTE: reviewer run 1 void — duplicate\n", "",
            ) + "\nreviewer round 3: APPROVE\n"
        )
        self.commit("fresh reviewer verdict")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.bundle()
        self.assertIn("already based", self.attest("refresh").stderr)

    def test_refresh_counts_authenticated_corrected_reviewer_completion(self):
        ticket = self.product / "factory/tickets/T-700.md"
        ticket.write_text(
            ticket.read_text()
            + "\nOPERATOR NOTE: reviewer run 1 void — duplicate\n",
            encoding="utf-8",
        )
        self.commit("void superseded reviewer")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        old_head = self.head()

        completed = []
        for role, receipt in (("reviewer", "1" * 64), ("narrator", "2" * 64)):
            manifest = self.product / f"factory/runs/{role}-1.meta"
            manifest.write_text(
                manifest.read_text()
                + f"contract_version=1.8.0\noutput_sha256={'3' * 64}\n"
                + f"transition_receipt_sha256={receipt}\n",
                encoding="utf-8",
            )
            completed.append({
                "contract_version": "1.8.0",
                "factory_sha": KIT_SHA,
                "head_before": self.reviewed,
                "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "output_sha256": "3" * 64,
                "role": role,
                "run_id": f"{role}-1",
                "transition_receipt_sha256": receipt,
            })
        corrected_receipt = "4" * 64
        corrected_factory = "e" * 40
        completed.append({
            "contract_version": "1.8.0",
            "factory_sha": corrected_factory,
            "head_before": self.reviewed,
            "manifest_sha256": "5" * 64,
            "output_sha256": "6" * 64,
            "role": "reviewer",
            "run_id": "reviewer-corrected",
            "transition_receipt_sha256": corrected_receipt,
        })
        correction = {
            "failed_factory_sha": corrected_factory,
            "issue": "https://github.com/nysa-company/software-factory/issues/390",
            "output_head_sha": old_head,
            "progress_events": 1,
            "progress_journal_sha256": "7" * 64,
            "receipt_parent_file_sha256": "8" * 64,
            "recovery_factory_sha": KIT_SHA,
            "role": "reviewer",
            "run_id": "reviewer-corrected",
            "schema": "nysa.software-factory.completed-role-correction/v2",
            "transition_receipt_sha256": corrected_receipt,
        }
        module = TICKET_ATTEST.passport_module()
        secret = b"p" * 32
        (self.controller / "passport.key").write_bytes(secret)
        (self.controller / "passport.key").chmod(0o600)
        passports = self.controller / "passports"
        passports.mkdir(mode=0o700)
        passport_body = {
            "branch": "ticket/T-700",
            "completed_role_corrections": [correction],
            "completed_role_evidence": completed,
            "contract_version": "1.8.0",
            "factory_release_history": [
                {"contract_version": "1.8.0", "factory_sha": corrected_factory},
                {"contract_version": "1.8.0", "factory_sha": KIT_SHA},
            ],
            "factory_sha": KIT_SHA,
            "head_sha": old_head,
            "migration_history": [],
            "project": "example-product",
            "schema": "nysa.software-factory.ticket-passport/v1",
            "ticket": "T-700",
        }
        passport = module.authenticate(passport_body, secret)
        passport_path = passports / "T-700.json"
        passport_path.write_bytes(module.canonical(passport))
        passport_path.chmod(0o600)
        without_correction = module.authenticate({
            **passport_body, "completed_role_corrections": [],
        }, secret)
        passport_path.write_bytes(module.canonical(without_correction))
        with (
            patch.dict(os.environ, self.env, clear=True),
            self.assertRaisesRegex(
                TICKET_ATTEST.Refusal,
                "authenticated completed-role artifact is missing",
            ),
        ):
            TICKET_ATTEST.completed_role_runs(
                self.product, "T-700",
                TICKET_ATTEST.successful_runs(
                    self.product, self.product, "T-700",
                ),
            )
        passport_path.write_bytes(module.canonical(passport))

        updater = self.temp / "corrected-review-main-update"
        command("git", "clone", "-q", "--branch", "main", str(self.remote), str(updater))
        (updater / "factory/QUALIFICATION.json").write_text(
            '{"generation": 2}\n', encoding="utf-8",
        )
        command("git", "add", ".", cwd=updater)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "advance protected control", cwd=updater,
        )
        command("git", "push", "-q", "origin", "main", cwd=updater)
        self.update_state(merge_state="UNKNOWN")

        result = self.attest("refresh")
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(
            (self.product / "factory/attestations/T-700/refresh.json").read_text()
        )
        self.assertEqual(receipt["prior_reviewer_runs"], 1)
        self.assertEqual(receipt["prior_approve_verdicts"], 1)
        self.assertEqual(receipt["prior_narrator_runs"], 1)

        refreshed = self.head()
        prior = self.product / "factory/runs/narrator-1.meta"
        current = self.product / "factory/runs/narrator-2.meta"
        current.write_text(
            prior.read_text()
            .replace("run_id=narrator-1", "run_id=narrator-2")
            .replace(f"role_head_before={self.reviewed}", f"role_head_before={refreshed}")
            .replace("terminal_at=2026-07-17T12:02:00Z", "terminal_at=2026-07-17T14:02:00Z")
            .replace(f"transition_receipt_sha256={'2' * 64}",
                     f"transition_receipt_sha256={'9' * 64}"),
            encoding="utf-8",
        )
        ledger = self.product / "factory/runtime-ledger.csv"
        ledger.write_text(
            ledger.read_text()
            + "2026-07-17,14:02:00,T-700,narrator,mock,1,1,0.1,0,narrator-2,"
            "anthropic,mock,pinned_route_plan,reported,1\n",
            encoding="utf-8",
        )
        completed.append({
            "contract_version": "1.8.0",
            "factory_sha": KIT_SHA,
            "head_before": refreshed,
            "manifest_sha256": hashlib.sha256(current.read_bytes()).hexdigest(),
            "output_sha256": "3" * 64,
            "role": "narrator",
            "run_id": "narrator-2",
            "transition_receipt_sha256": "9" * 64,
        })
        passport_body.update(
            completed_role_evidence=completed,
            head_sha=refreshed,
        )
        passport_path.write_bytes(module.canonical(
            module.authenticate(passport_body, secret)
        ))
        result = self.attest("bundle")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_receipt_only_refresh_replay_accepts_missing_rows(self):
        self.assert_receipt_only_refresh_replay("")

    def test_dependency_publication_replay_accepts_no_dependencies(self):
        self.assert_receipt_only_refresh_replay("", dependency_replay=True)

    def test_receipt_only_refresh_replay_accepts_unchecked_rows(self):
        self.assert_receipt_only_refresh_replay(
            "- [ ] Evidence bundle posted\n- [ ] Operator approved\n",
        )

    def test_receipt_only_refresh_replay_refuses_checked_row(self):
        self.assert_receipt_only_refresh_replay(
            "- [x] Evidence bundle posted\n- [ ] Operator approved\n",
            "changed unauthorized paths",
        )

    def test_receipt_only_refresh_replay_refuses_duplicate_row(self):
        self.assert_receipt_only_refresh_replay(
            "- [ ] Evidence bundle posted\n- [ ] Evidence bundle posted\n"
            "- [ ] Operator approved\n",
            "ticket reset is ambiguous",
        )

    def test_control_only_refresh_preserves_review_and_narrator(self):
        self.bundle()
        updater = self.temp / "control-main-update"
        command("git", "clone", "-q", "--branch", "main", str(self.remote), str(updater))
        (updater / "factory/KIT_PIN").write_text(KIT_SHA + "\n")
        (updater / "factory/QUALIFICATION.json").write_text('{"generation": 2}\n')
        migration = updater / f"factory/migrations/inflight-release/{KIT_SHA}.json"
        migration.parent.mkdir(parents=True)
        migration.write_text("{}\n")
        command("git", "add", ".", cwd=updater)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "advance protected control metadata", cwd=updater,
        )
        command("git", "push", "-q", "origin", "main", cwd=updater)
        self.update_state(merge_state="UNKNOWN")

        result = self.attest("refresh")
        self.assertEqual(result.returncode, 0, result.stderr)
        first_refresh = json.loads(
            (self.product / "factory/attestations/T-700/refresh.json").read_text()
        )
        self.assertEqual(first_refresh["revalidation_generation"], 1)
        result = self.attest("bundle")
        self.assertEqual(result.returncode, 0, result.stderr)
        attestation = json.loads(
            (self.product / "factory/attestations/T-700/bundle.json").read_text()
        )
        self.assertEqual(attestation["reviewer_run_id"], "reviewer-1")
        self.assertEqual(attestation["narrator_run_id"], "narrator-1")

        (updater / "factory/unknown-control.json").write_text("{}\n")
        command("git", "add", ".", cwd=updater)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "advance unknown factory metadata", cwd=updater,
        )
        command("git", "push", "-q", "origin", "main", cwd=updater)
        self.update_state(merge_state="UNKNOWN")
        result = self.attest("refresh")
        self.assertEqual(result.returncode, 0, result.stderr)
        second_refresh = json.loads(
            (self.product / "factory/attestations/T-700/refresh.json").read_text()
        )
        self.assertEqual(second_refresh["generation"], 2)
        self.assertEqual(second_refresh["revalidation_generation"], 1)
        self.assertEqual(second_refresh["revalidation_factory_sha"], KIT_SHA)
        self.assertIn("post-refresh Reviewer", self.attest("bundle").stderr)
        second_refresh["revalidation_generation"] = 2
        refresh_path = (
            self.product / "factory/attestations/T-700/refresh.json"
        )
        refresh_path.write_text(
            json.dumps(second_refresh, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        command("git", "add", str(refresh_path), cwd=self.product)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "--amend", "-qm", "change refresh reservation generation",
            cwd=self.product,
        )
        command(
            "git", "push", "-q", "--force", "origin", "ticket/T-700",
            cwd=self.product,
        )
        self.assertIn(
            "revalidation generation is not continuous",
            self.attest("bundle").stderr,
        )

    def test_disjoint_refresh_preserves_but_overlap_invalidates_review(self):
        self.bundle()
        updater = self.temp / "disjoint-main-update"
        command("git", "clone", "-q", "--branch", "main", str(self.remote), str(updater))
        (updater / "sibling.txt").write_text("unrelated protected change\n")
        (updater / "factory/tickets/T-701.md").write_text("# T-701\n")
        (updater / "factory/route-plans/T-701.json").write_text("{}\n")
        ledger = updater / "factory/ledger.csv"
        ledger.write_text(ledger.read_text() + (
            "2026-08-19,00:00:00,T-701,narrator,mock,v1,1,0.000000,0,"
            "sibling-run,anthropic,mock,pinned_route_plan,provider_reported,1\n"
        ))
        sibling_attestations = updater / "factory/attestations/T-701"
        sibling_attestations.mkdir(parents=True)
        (sibling_attestations / "done.json").write_text("{}\n")
        command("git", "add", ".", cwd=updater)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "merge unrelated sibling", cwd=updater,
        )
        command("git", "push", "-q", "origin", "main", cwd=updater)
        self.update_state(merge_state="UNKNOWN")

        refreshed = self.attest("refresh")
        self.assertEqual(refreshed.returncode, 0, refreshed.stderr)
        rebundled = self.attest("bundle")
        self.assertEqual(rebundled.returncode, 0, rebundled.stderr)
        attestation = json.loads(
            (self.product / "factory/attestations/T-700/bundle.json").read_text()
        )
        self.assertEqual(attestation["reviewer_run_id"], "reviewer-1")
        self.assertEqual(attestation["narrator_run_id"], "narrator-1")

        (updater / "app.txt").write_text("reviewed code\n")
        command("git", "add", ".", cwd=updater)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "merge overlapping sibling", cwd=updater,
        )
        command("git", "push", "-q", "origin", "main", cwd=updater)
        self.update_state(merge_state="UNKNOWN")
        refreshed = self.attest("refresh")
        self.assertEqual(refreshed.returncode, 0, refreshed.stderr)
        self.assertIn("post-refresh Reviewer", self.attest("bundle").stderr)

    def test_refresh_resolves_only_exact_successor_ticket_pin_conflict(self):
        old_kit = "b" * 40
        ticket = self.product / "factory/tickets/T-700.md"

        command("git", "switch", "-q", "main", cwd=self.product)
        ticket.write_text(
            self.ticket("Review").replace(
                "Priority: normal",
                f"Kit-SHA: {old_kit}\nFocused-Check: old\nPriority: normal",
            )
        )
        (self.product / "factory/KIT_PIN").write_text(old_kit + "\n")
        self.commit("record source release")
        command("git", "push", "-q", "origin", "main", cwd=self.product)

        command("git", "switch", "-q", "ticket/T-700", cwd=self.product)
        command("git", "merge", "-q", "--no-edit", "main", cwd=self.product)
        ticket.write_text(
            ticket.read_text()
            .replace(old_kit, KIT_SHA)
            .replace("Focused-Check: old", "Focused-Check: new")
        )
        (self.product / "factory/KIT_PIN").write_text(KIT_SHA + "\n")
        self.commit("migrate ticket release")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        old_head = self.head()

        updater = self.temp / "successor-pin-main"
        command("git", "clone", "-q", "--branch", "main", str(self.remote), str(updater))
        protected_ticket = updater / "factory/tickets/T-700.md"
        protected_ticket.write_text(protected_ticket.read_text().replace(old_kit, KIT_SHA))
        (updater / "factory/KIT_PIN").write_text(KIT_SHA + "\n")
        command("git", "add", ".", cwd=updater)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "authorize successor release", cwd=updater,
        )
        protected = self.head_at(updater)
        command("git", "push", "-q", "origin", "main", cwd=updater)
        self.update_state(merge_state="UNKNOWN")

        refreshed = self.attest("refresh")
        self.assertEqual(refreshed.returncode, 0, refreshed.stderr)
        self.assertIn("Focused-Check: new", ticket.read_text())
        merge = json.loads(refreshed.stdout)["attestation"]["merge_head"]
        self.assertEqual(
            command(
                "git", "rev-list", "--parents", "-n", "1", merge,
                cwd=self.product,
            ).stdout.split(),
            [merge, old_head, protected],
        )

        command("git", "reset", "--hard", "-q", old_head, cwd=self.product)
        command("git", "push", "-q", "--force", "origin", "ticket/T-700", cwd=self.product)
        extra = self.temp / "unsafe-successor-pin-main"
        command("git", "clone", "-q", "--branch", "main", str(self.remote), str(extra))
        extra_ticket = extra / "factory/tickets/T-700.md"
        extra_ticket.write_text(extra_ticket.read_text() + "\nprotected content change\n")
        command("git", "add", ".", cwd=extra)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "change protected ticket content", cwd=extra,
        )
        command("git", "push", "-q", "origin", "main", cwd=extra)
        self.update_state(merge_state="UNKNOWN")
        refused = self.attest("refresh")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("protected main conflicts", refused.stderr)
        self.assertEqual(self.head(), old_head)

    def test_successor_ticket_pin_conflict_accepts_only_trailing_blank_normalization(self):
        work = self.temp / "trailing-pin-conflict"
        command("git", "init", "-q", "-b", "main", str(work))
        command("git", "config", "user.name", "test", cwd=work)
        command("git", "config", "user.email", "test@example.com", cwd=work)
        ticket = work / "factory/tickets/T-800.md"
        ticket.parent.mkdir(parents=True)
        ticket.write_text(
            f"# T-800\nKit-SHA: {'b' * 40}\nFocused-Check: old\n\n"
        )
        (work / "factory/KIT_PIN").write_text("b" * 40 + "\n")
        command("git", "add", ".", cwd=work)
        command("git", "commit", "-qm", "source", cwd=work)

        command("git", "switch", "-qc", "ticket/T-800", cwd=work)
        ticket.write_text(
            ticket.read_text()
            .replace("b" * 40, KIT_SHA)
            .replace("Focused-Check: old", "Focused-Check: new")
        )
        (work / "factory/KIT_PIN").write_text(KIT_SHA + "\n")
        command("git", "add", ".", cwd=work)
        command("git", "commit", "-qm", "ticket migration", cwd=work)
        ticket_head = self.head_at(work)

        command("git", "switch", "-q", "main", cwd=work)
        ticket.write_text(ticket.read_text().replace("b" * 40, KIT_SHA).rstrip() + "\n")
        (work / "factory/KIT_PIN").write_text(KIT_SHA + "\n")
        command("git", "add", ".", cwd=work)
        command("git", "commit", "-qm", "protected migration", cwd=work)
        protected = self.head_at(work)
        command("git", "switch", "-q", "ticket/T-800", cwd=work)
        self.assertNotEqual(
            command("git", "merge", "--no-ff", "--no-edit", protected, cwd=work, check=False).returncode,
            0,
        )
        self.assertTrue(
            TICKET_ATTEST.resolve_successor_ticket_pin_conflict(
                work, "T-800", KIT_SHA,
            )
        )
        merge = self.head_at(work)
        self.assertEqual(
            command("git", "rev-list", "--parents", "-n", "1", merge, cwd=work).stdout.split(),
            [merge, ticket_head, protected],
        )

    def test_control_only_refresh_invalidates_orphaned_review_lineage(self):
        tree = command(
            "git", "rev-parse", "HEAD^{tree}", cwd=self.product,
        ).stdout.strip()
        base = command(
            "git", "rev-parse", "origin/main", cwd=self.product,
        ).stdout.strip()
        orphan_reviewer = command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit-tree", tree, "-p", base, "-m", "orphan reviewer",
            cwd=self.product,
        ).stdout.strip()
        orphan_narrator = command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit-tree", tree, "-p", orphan_reviewer, "-m", "orphan narrator",
            cwd=self.product,
        ).stdout.strip()
        for role, head in (
            ("reviewer", orphan_reviewer),
            ("narrator", orphan_narrator),
        ):
            manifest = self.product / f"factory/runs/{role}-1.meta"
            manifest.write_text("\n".join(
                f"role_head_before={head}"
                if line.startswith("role_head_before=") else line
                for line in manifest.read_text().splitlines()
            ) + "\n")

        updater = self.temp / "orphan-control-main-update"
        command("git", "clone", "-q", "--branch", "main", str(self.remote), str(updater))
        (updater / "factory/QUALIFICATION.json").write_text('{"generation": 2}\n')
        migration = updater / f"factory/migrations/inflight-release/{KIT_SHA}.json"
        migration.parent.mkdir(parents=True)
        migration.write_text("{}\n")
        command("git", "add", ".", cwd=updater)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "advance protected control metadata", cwd=updater,
        )
        command("git", "push", "-q", "origin", "main", cwd=updater)
        self.update_state(merge_state="UNKNOWN")

        result = self.attest("refresh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("new post-refresh Reviewer", self.attest("bundle").stderr)

    def test_refresh_refuses_symlink_attestation_path(self):
        attestation = self.product / "factory/attestations/T-700"
        attestation.mkdir(parents=True)
        external = self.temp / "external.json"
        external.write_text("unchanged\n")
        (attestation / "refresh.json").symlink_to(external)
        self.commit("malicious refresh symlink")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        updater = self.temp / "symlink-main-update"
        command("git", "clone", "-q", "--branch", "main", str(self.remote), str(updater))
        (updater / "main.txt").write_text("protected update\n")
        command("git", "add", ".", cwd=updater)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "advance protected main", cwd=updater,
        )
        command("git", "push", "-q", "origin", "main", cwd=updater)

        self.assertIn("attestation path is unsafe", self.attest("refresh").stderr)
        self.assertEqual(external.read_text(), "unchanged\n")

    def test_dependency_refresh_needs_no_pr_and_preserves_building_state(self):
        ticket = self.product / "factory/tickets/T-700.md"
        ticket.write_text(
            ticket.read_text()
            .replace("State: Review", "State: Building")
            .replace("Priority: normal", "Priority: normal\nDepends-On: T-094")
        )
        self.commit("record failed repair state")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        updater = self.temp / "building-main-update"
        command("git", "clone", "-q", "--branch", "main", str(self.remote), str(updater))
        (updater / "main.txt").write_text("protected update\n")
        command("git", "add", ".", cwd=updater)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "advance protected main", cwd=updater,
        )
        protected = self.head_at(updater)
        command("git", "push", "-q", "origin", "main", cwd=updater)
        self.env["FACTORY_TRANSITION_STAGE"] = (
            "REFUSE dependency refresh required; "
            f"dependencies=T-094; protected-main={protected}"
        )
        args = argparse.Namespace(ticket="T-700")
        with (
            patch.dict(os.environ, self.env, clear=True),
            patch.object(
                TICKET_ATTEST, "protected_dependency",
                return_value={"basis": "normal", "ticket": "T-094"},
            ),
            patch.object(
                TICKET_ATTEST, "gh",
                side_effect=AssertionError("dependency refresh queried a PR"),
            ),
        ):
            result = TICKET_ATTEST.dependency_refresh(
                args, self.product, self.product, "ticket/", str(self.remote),
                KIT_SHA,
            )
        self.assertEqual(result["action"], "dependency-refresh")
        self.assertIn("State: Building", ticket.read_text())
        receipt = json.loads(
            (
                self.product
                / "factory/attestations/T-700/dependency-refresh.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(receipt["protected_head"], protected)
        self.assertEqual(receipt["preserved_state"], "Building")
        self.assertEqual(receipt["dependencies"], ["T-094"])
        self.assertEqual(
            command(
                "git", "merge-base", "--is-ancestor", protected, "HEAD",
                cwd=self.product, check=False,
            ).returncode,
            0,
        )

    def test_dependency_refresh_retires_bundle_and_approval_after_publication(self):
        ticket = self.product / "factory/tickets/T-700.md"
        ticket.write_text(ticket.read_text().replace(
            "Priority: normal", "Priority: normal\nDepends-On: T-094",
        ))
        self.commit("declare publication dependency")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.bundle()
        self.approval_overlay()
        approved = self.attest("approval")
        self.assertEqual(approved.returncode, 0, approved.stderr)
        self.approval_overlay()
        old_head = self.head()
        attestation_dir = self.product / "factory/attestations/T-700"
        prior_bundle = TICKET_ATTEST.blob_id(
            self.product, attestation_dir / "bundle.json",
        )
        prior_approval = TICKET_ATTEST.blob_id(
            self.product, attestation_dir / "approval.json",
        )

        updater = self.temp / "approved-dependency-main-update"
        command("git", "clone", "-q", "--branch", "main", str(self.remote), str(updater))
        (updater / "dependency.txt").write_text("terminal dependency\n")
        (updater / "factory/tickets/T-094.md").write_text(
            "# T-094\n\nState: Done\n"
        )
        command("git", "add", ".", cwd=updater)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "complete dependency", cwd=updater,
        )
        protected = self.head_at(updater)
        command("git", "push", "-q", "origin", "main", cwd=updater)
        self.env["FACTORY_TRANSITION_STAGE"] = (
            "REFUSE dependency refresh required; "
            f"dependencies=T-094; protected-main={protected}"
        )
        args = argparse.Namespace(ticket="T-700")
        with (
            patch.dict(os.environ, self.env, clear=True),
            patch.object(
                TICKET_ATTEST, "protected_dependency",
                return_value={"basis": "normal", "ticket": "T-094"},
            ),
        ):
            os.environ["FACTORY_TEST_REFRESH_CRASH_AFTER_PUSH"] = "1"
            with self.assertRaises(SystemExit):
                TICKET_ATTEST.dependency_refresh(
                    args, self.product, self.product, "ticket/", str(self.remote),
                    KIT_SHA,
                )
            os.environ.pop("FACTORY_TEST_REFRESH_CRASH_AFTER_PUSH")
            refreshed_head = self.head()
            self.assertNotEqual(
                command(
                    "git", "rev-parse", "refs/remotes/origin/ticket/T-700",
                    cwd=self.product,
                ).stdout.strip(),
                refreshed_head,
            )
            self.assertEqual(
                json.loads(
                    (self.product / "factory/operator-map.json").read_text()
                )["tickets"]["T-700"]["operator"]["approval"],
                "Receipt",
            )
            os.environ.pop("FACTORY_TRANSITION_STAGE", None)
            os.environ.pop("FACTORY_TRANSITION_RECEIPT_SHA256", None)
            replayed = TICKET_ATTEST.dependency_publication_replay(
                args, self.product, self.product, "ticket/", str(self.remote),
            )

        self.assertEqual(replayed["action"], "dependency-publication-refresh")
        self.assertEqual(self.head(), refreshed_head)
        self.assertIsNone(
            TICKET_ATTEST.stale_approval_overlay_version(
                self.product, "T-700",
            )
        )
        self.assertEqual(replayed["dependencies"], ["T-094"])
        self.assertEqual(replayed["attestation"]["old_head"], old_head)
        self.assertEqual(replayed["attestation"]["base_head"], protected)
        self.assertEqual(replayed["attestation"]["prior_bundle_blob"], prior_bundle)
        self.assertEqual(replayed["attestation"]["prior_approval_blob"], prior_approval)
        self.assertFalse((attestation_dir / "bundle.json").exists())
        self.assertFalse((attestation_dir / "approval.json").exists())
        refreshed = ticket.read_text()
        self.assertIn("State: Review", refreshed)
        self.assertNotIn("Operator-Approval:", refreshed)
        self.assertIn("- [ ] Evidence bundle posted", refreshed)
        self.assertIn("- [ ] Operator approved", refreshed)
        self.assertIn("post-refresh Reviewer", self.attest("bundle").stderr)

    def test_publication_dependency_refresh_waits_without_retiring_evidence(self):
        ticket = self.product / "factory/tickets/T-700.md"
        ticket.write_text(ticket.read_text().replace(
            "Priority: normal", "Priority: normal\nDepends-On: T-094",
        ))
        self.commit("declare dependency before bundle")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.bundle()
        bundle = self.product / "factory/attestations/T-700/bundle.json"
        old_head = self.head()
        expected = command(
            "git", "rev-parse", "origin/main", cwd=self.product,
        ).stdout.strip()
        updater = self.temp / "moved-dependency-main"
        command("git", "clone", "-q", "--branch", "main", str(self.remote), str(updater))
        (updater / "moved.txt").write_text("newer protected main\n")
        command("git", "add", ".", cwd=updater)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "move protected main", cwd=updater,
        )
        observed = self.head_at(updater)
        command("git", "push", "-q", "origin", "main", cwd=updater)
        self.env["FACTORY_TRANSITION_STAGE"] = (
            "REFUSE dependency refresh required; "
            f"dependencies=T-094; protected-main={expected}"
        )
        args = argparse.Namespace(ticket="T-700")
        with patch.dict(os.environ, self.env, clear=True):
            result = TICKET_ATTEST.dependency_refresh(
                args, self.product, self.product, "ticket/", str(self.remote),
                KIT_SHA,
            )
        self.assertEqual(result, {
            "action": "dependency-wait",
            "expected_protected_head": expected,
            "observed_protected_head": observed,
        })
        self.assertEqual(self.head(), old_head)
        self.assertTrue(bundle.is_file())

    def test_dependency_publication_replay_refuses_dependency_drift_before_overlay(self):
        ticket = self.product / "factory/tickets/T-700.md"
        ticket.write_text(
            "# T-700\n\nState: Review\nDepends-On: T-095\n",
            encoding="utf-8",
        )
        old_ticket = "# T-700\n\nState: Review\nDepends-On: T-094\n"
        completed = subprocess.CompletedProcess(
            ["git"], 0, old_ticket, "",
        )
        with (
            patch.object(
                TICKET_ATTEST, "ensure_clean_branch", return_value="a" * 40,
            ),
            patch.object(
                TICKET_ATTEST, "load_refresh_receipt",
                return_value=({"old_head": "b" * 40}, "a" * 40),
            ),
            patch.object(TICKET_ATTEST, "git", return_value=completed),
            patch.object(
                TICKET_ATTEST, "consume_stale_approval_overlay",
            ) as consume,
            self.assertRaisesRegex(
                TICKET_ATTEST.Refusal, "replay dependencies changed",
            ),
        ):
            TICKET_ATTEST.dependency_publication_replay(
                argparse.Namespace(ticket="T-700"), self.product,
                self.product, "ticket/", str(self.remote),
            )
        consume.assert_not_called()

    def test_dependency_refresh_retires_bundle_before_approval(self):
        ticket = self.product / "factory/tickets/T-700.md"
        ticket.write_text(ticket.read_text().replace(
            "Priority: normal", "Priority: normal\nDepends-On: T-094",
        ))
        self.commit("declare dependency before publication")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.bundle()
        attestation_dir = self.product / "factory/attestations/T-700"
        prior_bundle = TICKET_ATTEST.blob_id(
            self.product, attestation_dir / "bundle.json",
        )
        updater = self.temp / "bundle-dependency-main-update"
        command("git", "clone", "-q", "--branch", "main", str(self.remote), str(updater))
        (updater / "dependency-done.txt").write_text("done\n")
        command("git", "add", ".", cwd=updater)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "complete dependency after bundle", cwd=updater,
        )
        protected = self.head_at(updater)
        command("git", "push", "-q", "origin", "main", cwd=updater)
        self.env["FACTORY_TRANSITION_STAGE"] = (
            "REFUSE dependency refresh required; "
            f"dependencies=T-094; protected-main={protected}"
        )
        with (
            patch.dict(os.environ, self.env, clear=True),
            patch.object(
                TICKET_ATTEST, "protected_dependency",
                return_value={"basis": "normal", "ticket": "T-094"},
            ),
        ):
            result = TICKET_ATTEST.dependency_refresh(
                argparse.Namespace(ticket="T-700"), self.product,
                self.product, "ticket/", str(self.remote),
                KIT_SHA,
            )
        self.assertEqual(result["action"], "dependency-publication-refresh")
        self.assertEqual(
            result["attestation"]["schema"],
            "nysa.software-factory.ticket-refresh/v2",
        )
        self.assertEqual(
            result["attestation"]["revalidation_factory_sha"], KIT_SHA,
        )
        self.assertEqual(
            result["attestation"]["revalidation_budget_micro_usd"],
            20_000_000,
        )
        self.assertEqual(result["attestation"]["prior_bundle_blob"], prior_bundle)
        self.assertIsNone(result["attestation"]["prior_approval_blob"])
        self.assertFalse((attestation_dir / "bundle.json").exists())
        self.assertIn("State: Review", ticket.read_text())

    def test_dependency_refresh_routes_regular_test_conflict_to_test_author(self):
        command("git", "switch", "-q", "main", cwd=self.product)
        test_path = self.product / "tests/dependency-conflict.test.ts"
        test_path.parent.mkdir()
        test_path.write_text("expect('base')\n", encoding="utf-8")
        self.commit("add protected test baseline")
        command("git", "push", "-q", "origin", "main", cwd=self.product)
        command("git", "switch", "-q", "ticket/T-700", cwd=self.product)
        command("git", "merge", "-q", "--no-edit", "main", cwd=self.product)

        ticket = self.product / "factory/tickets/T-700.md"
        ticket.write_text(
            ticket.read_text()
            .replace("State: Review", "State: Building")
            .replace("Priority: normal", "Priority: normal\nDepends-On: T-094")
        )
        test_path.write_text("expect('ticket contract')\n", encoding="utf-8")
        self.commit("author ticket acceptance test")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        old_head = self.head()
        old_blob = command(
            "git", "rev-parse", f"{old_head}:tests/dependency-conflict.test.ts",
            cwd=self.product,
        ).stdout.strip()

        updater = self.temp / "conflicting-test-main"
        command("git", "clone", "-q", "--branch", "main", str(self.remote), str(updater))
        (updater / "tests/dependency-conflict.test.ts").write_text(
            "expect('protected component contract')\n", encoding="utf-8",
        )
        command("git", "add", ".", cwd=updater)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "advance protected test", cwd=updater,
        )
        protected = self.head_at(updater)
        protected_blob = command(
            "git", "rev-parse",
            f"{protected}:tests/dependency-conflict.test.ts", cwd=updater,
        ).stdout.strip()
        command("git", "push", "-q", "origin", "main", cwd=updater)
        transition = "f" * 64
        self.env.update({
            "FACTORY_CONTRACT_VERSION": "1.8.0",
            "FACTORY_TRANSITION_RECEIPT_SHA256": transition,
            "FACTORY_TRANSITION_STAGE": (
                "REFUSE dependency refresh required; "
                f"dependencies=T-094; protected-main={protected}"
            ),
        })
        args = argparse.Namespace(ticket="T-700")
        with (
            patch.dict(os.environ, self.env, clear=True),
            patch.object(
                TICKET_ATTEST, "protected_dependency",
                return_value={"basis": "normal", "ticket": "T-094"},
            ),
        ):
            result = TICKET_ATTEST.dependency_refresh(
                args, self.product, self.product, "ticket/", str(self.remote),
                KIT_SHA,
            )

        self.assertEqual(result["action"], "dependency-conflict-refresh")
        receipt = result["attestation"]
        self.assertEqual(receipt["schema"], "nysa.software-factory.dependency-refresh/v2")
        self.assertEqual(receipt["repair_owner"], "test-author")
        self.assertEqual(receipt["transition_receipt_sha256"], transition)
        self.assertEqual(receipt["conflicts"], [{
            "path": "tests/dependency-conflict.test.ts",
            "base_blob": receipt["conflicts"][0]["base_blob"],
            "base_mode": "100644",
            "ticket_blob": old_blob,
            "ticket_mode": "100644",
            "protected_blob": protected_blob,
            "protected_mode": "100644",
        }])
        self.assertEqual(test_path.read_text(), "expect('protected component contract')\n")
        self.assertEqual(
            command(
                "git", "merge-base", "--is-ancestor", protected, "HEAD",
                cwd=self.product, check=False,
            ).returncode,
            0,
        )
        merge = receipt["merge_head"]
        self.assertEqual(
            command(
                "git", "rev-list", "--parents", "-n", "1", merge,
                cwd=self.product,
            ).stdout.split(),
            [merge, old_head, protected],
        )
        receipt_path = (
            self.product
            / "factory/attestations/T-700/dependency-refresh.json"
        )
        with self.assertRaisesRegex(
            TICKET_ATTEST.Refusal,
            "dependency refresh receipt is malformed",
        ):
            TICKET_ATTEST.dependency_refresh_generation(
                receipt_path, "T-999",
            )

    def test_dependency_refresh_restores_branch_for_non_test_conflict(self):
        command("git", "switch", "-q", "main", cwd=self.product)
        app_path = self.product / "src/conflict.ts"
        app_path.parent.mkdir()
        app_path.write_text("export const value = 'base';\n", encoding="utf-8")
        self.commit("add application baseline")
        command("git", "push", "-q", "origin", "main", cwd=self.product)
        command("git", "switch", "-q", "ticket/T-700", cwd=self.product)
        command("git", "merge", "-q", "--no-edit", "main", cwd=self.product)
        ticket = self.product / "factory/tickets/T-700.md"
        ticket.write_text(
            ticket.read_text()
            .replace("State: Review", "State: Building")
            .replace("Priority: normal", "Priority: normal\nDepends-On: T-094")
        )
        app_path.write_text("export const value = 'ticket';\n", encoding="utf-8")
        self.commit("change ticket application")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        old_head = self.head()

        updater = self.temp / "conflicting-application-main"
        command("git", "clone", "-q", "--branch", "main", str(self.remote), str(updater))
        (updater / "src/conflict.ts").write_text(
            "export const value = 'protected';\n", encoding="utf-8",
        )
        command("git", "add", ".", cwd=updater)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "advance protected application", cwd=updater,
        )
        protected = self.head_at(updater)
        command("git", "push", "-q", "origin", "main", cwd=updater)
        self.env.update({
            "FACTORY_CONTRACT_VERSION": "1.8.0",
            "FACTORY_TRANSITION_RECEIPT_SHA256": "f" * 64,
            "FACTORY_TRANSITION_STAGE": (
                "REFUSE dependency refresh required; "
                f"dependencies=T-094; protected-main={protected}"
            ),
        })
        args = argparse.Namespace(ticket="T-700")
        with (
            patch.dict(os.environ, self.env, clear=True),
            patch.object(
                TICKET_ATTEST, "protected_dependency",
                return_value={"basis": "normal", "ticket": "T-094"},
            ),
            self.assertRaisesRegex(
                TICKET_ATTEST.Refusal, "not test-author-owned",
            ),
        ):
            TICKET_ATTEST.dependency_refresh(
                args, self.product, self.product, "ticket/", str(self.remote),
                KIT_SHA,
            )
        self.assertEqual(self.head(), old_head)
        self.assertEqual(
            command(
                "git", "status", "--porcelain=v1", "-z", cwd=self.product,
            ).stdout,
            "",
        )

    def test_refresh_detects_pr_merge_race_after_push(self):
        updater = self.temp / "race-main-update"
        command("git", "clone", "-q", "--branch", "main", str(self.remote), str(updater))
        (updater / "main.txt").write_text("protected update\n")
        command("git", "add", ".", cwd=updater)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "advance protected main", cwd=updater,
        )
        command("git", "push", "-q", "origin", "main", cwd=updater)
        self.write_state(merge_on_second_open=True)

        result = self.attest("refresh")

        self.assertIn("expected exactly one open PR", result.stderr)
        self.assertTrue((
            self.product / "factory/attestations/T-700/refresh.json"
        ).is_file())

    def test_bundle_refuses_deleted_historical_refresh_receipt(self):
        updater = self.temp / "deleted-refresh-main-update"
        command("git", "clone", "-q", "--branch", "main", str(self.remote), str(updater))
        (updater / "main.txt").write_text("protected update\n")
        command("git", "add", ".", cwd=updater)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "advance protected main", cwd=updater,
        )
        command("git", "push", "-q", "origin", "main", cwd=updater)
        result = self.attest("refresh")
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = self.product / "factory/attestations/T-700/refresh.json"
        receipt.unlink()
        self.commit("delete refresh receipt")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)

        self.assertIn("refresh receipt is missing", self.attest("bundle").stderr)
        (updater / "main-2.txt").write_text("second protected update\n")
        command("git", "add", ".", cwd=updater)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "advance protected main again", cwd=updater,
        )
        command("git", "push", "-q", "origin", "main", cwd=updater)
        self.assertIn("historical refresh receipt", self.attest("refresh").stderr)

        old_head = self.head()
        base_head = self.head_at(updater)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "merge", "--no-ff", "--no-edit", base_head, cwd=self.product,
        )
        merge_head = self.head()
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps({
            "schema": "nysa.software-factory.ticket-refresh/v1",
            "ticket": "T-700",
            "generation": 1,
            "old_head": old_head,
            "base_head": base_head,
            "merge_head": merge_head,
            "prior_reviewer_runs": 1,
            "prior_approve_verdicts": 1,
            "prior_request_changes_verdicts": 0,
            "prior_narrator_runs": 1,
            "prior_bundle_blob": None,
            "prior_approval_blob": None,
            "refreshed_at": "2026-07-17T14:00:00Z",
        }, indent=2, sort_keys=True) + "\n")
        self.commit("forge reset refresh generation")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.assertIn(
            "prior refresh receipt is missing from the recorded old head",
            self.attest("bundle").stderr,
        )

    def test_bundle_refuses_forged_refresh_generation_and_topology(self):
        old_head = self.head()
        receipt = self.product / "factory/attestations/T-700/refresh.json"
        receipt.parent.mkdir(parents=True)
        forged = {
            "schema": "nysa.software-factory.ticket-refresh/v1",
            "ticket": "T-700",
            "generation": 0,
            "old_head": old_head,
            "base_head": command(
                "git", "rev-parse", "origin/main", cwd=self.product,
            ).stdout.strip(),
            "merge_head": old_head,
            "prior_reviewer_runs": 1,
            "prior_approve_verdicts": 1,
            "prior_request_changes_verdicts": 0,
            "prior_narrator_runs": 1,
            "prior_bundle_blob": None,
            "prior_approval_blob": None,
            "refreshed_at": "2026-07-17T14:00:00Z",
        }
        receipt.write_text(json.dumps(forged, indent=2, sort_keys=True) + "\n")
        self.commit("forge refresh receipt")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.assertIn("identity or baselines", self.attest("bundle").stderr)

        forged["generation"] = 1
        receipt.write_text(json.dumps(forged, indent=2, sort_keys=True) + "\n")
        command("git", "add", str(receipt), cwd=self.product)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "--amend", "-qm", "forge refresh receipt", cwd=self.product,
        )
        command("git", "push", "-q", "--force", "origin", "ticket/T-700", cwd=self.product)
        self.assertIn("refresh merge topology", self.attest("bundle").stderr)

    def test_bundle_refuses_noncontinuous_refresh_generation(self):
        updater = self.temp / "generation-main-update"
        command("git", "clone", "-q", "--branch", "main", str(self.remote), str(updater))
        (updater / "main.txt").write_text("protected update\n")
        command("git", "add", ".", cwd=updater)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "advance protected main", cwd=updater,
        )
        command("git", "push", "-q", "origin", "main", cwd=updater)
        self.assertEqual(self.attest("refresh").returncode, 0)
        receipt = self.product / "factory/attestations/T-700/refresh.json"
        value = json.loads(receipt.read_text())
        value["generation"] = 2
        receipt.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        command("git", "add", str(receipt), cwd=self.product)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "--amend", "-qm", "record ticket refresh", cwd=self.product,
        )
        command("git", "push", "-q", "--force", "origin", "ticket/T-700", cwd=self.product)
        self.assertIn("generation is not continuous", self.attest("bundle").stderr)

    def test_refresh_refuses_duplicate_generation_in_prior_receipt(self):
        updater = self.temp / "duplicate-generation-main-update"
        command("git", "clone", "-q", "--branch", "main", str(self.remote), str(updater))
        (updater / "main.txt").write_text("first protected update\n")
        command("git", "add", ".", cwd=updater)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "advance protected main", cwd=updater,
        )
        command("git", "push", "-q", "origin", "main", cwd=updater)
        self.assertEqual(self.attest("refresh").returncode, 0)
        receipt = self.product / "factory/attestations/T-700/refresh.json"
        receipt.write_text(receipt.read_text().replace(
            '  "generation": 1,\n', '  "generation": 1,\n  "generation": 7,\n',
        ))
        self.commit("duplicate refresh generation")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        (updater / "main-2.txt").write_text("second protected update\n")
        command("git", "add", ".", cwd=updater)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "advance protected main again", cwd=updater,
        )
        command("git", "push", "-q", "origin", "main", cwd=updater)
        self.assertIn("existing refresh receipt is malformed", self.attest("refresh").stderr)

    def prepare_done(self, **state):
        self.bundle()
        self.approval_overlay()
        self.assertEqual(self.attest("approval").returncode, 0)
        merge_sha = self.head()
        command("git", "branch", "-f", "main", merge_sha, cwd=self.product)
        command("git", "push", "-q", "origin", f"{merge_sha}:refs/heads/main", cwd=self.product)
        self.workdir = self.temp / "closeout"
        command(
            "git", "worktree", "add", "-q", "-b", "chore/t700-closeout",
            str(self.workdir), "origin/main", cwd=self.product,
        )
        command("git", "push", "-q", "-u", "origin", "chore/t700-closeout", cwd=self.workdir)
        self.env["FAKE_WORKDIR"] = str(self.workdir)
        merged_state = {
            "merged": True, "merge_sha": merge_sha, "pr_head": merge_sha,
        }
        merged_state.update(state)
        self.write_state(**merged_state)

    def prepare_emergency(self, *, passport=True, paused=False, **state):
        merge_sha = self.head()
        command("git", "branch", "-f", "main", merge_sha, cwd=self.product)
        command("git", "push", "-q", "origin", f"{merge_sha}:refs/heads/main", cwd=self.product)
        self.workdir = self.temp / "emergency-closeout"
        command(
            "git", "worktree", "add", "-q", "-b", "chore/t700-closeout",
            str(self.workdir), "origin/main", cwd=self.product,
        )
        self.env["FAKE_WORKDIR"] = str(self.workdir)
        self.controller_state = self.temp / "controller"
        self.controller_state.mkdir(mode=0o700, exist_ok=True)
        self.controller_state.chmod(0o700)
        self.controller_state = self.controller_state.resolve()
        (self.controller_state / "passports").mkdir(mode=0o700, exist_ok=True)
        (self.controller_state / "claims").mkdir(mode=0o700, exist_ok=True)
        self.env.update({
            "FACTORY_CONTROLLER_STATE_DIR": str(self.controller_state),
            "FACTORY_PROJECT": "test-product",
        })
        if passport:
            key = b"p" * 32
            (self.controller_state / "passport.key").write_bytes(key)
            (self.controller_state / "passport.key").chmod(0o600)
            value = {
                "schema": "nysa.software-factory.ticket-passport/v1",
                "ticket": "T-700", "project": "test-product",
                "branch": "ticket/T-700", "current_state": "Review",
                "publication_state": "none", "factory_sha": KIT_SHA,
                "head_sha": merge_sha,
                "factory_release_history": [{
                    "contract_version": "1.8.0", "factory_sha": KIT_SHA,
                }],
            }
            canonical = lambda item: (json.dumps(
                item, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
            ) + "\n").encode()
            value["authentication_sha256"] = hmac.new(
                key, canonical(value), hashlib.sha256,
            ).hexdigest()
            value["passport_sha256"] = hashlib.sha256(canonical(value)).hexdigest()
            passport_path = self.controller_state / "passports/T-700.json"
            passport_path.write_bytes(canonical(value))
            passport_path.chmod(0o600)
            claim_path = self.controller_state / "claims/T-700.json"
            claim_path.write_text(json.dumps({
                "schema": "nysa.software-factory.controller-claim/v1",
                "ticket": "T-700", "branch": "ticket/T-700",
                "status": "blocked", "parked": True, "lease": "",
                "publication_lease": "", "role": "reviewer",
                "blocked_reason": "controller-error", "receipt": "1" * 64,
            }))
            claim_path.chmod(0o600)
            if paused:
                claim_path.unlink()
                pause = {
                    "blocking_issue": "https://github.com/acme/factory/issues/269",
                    "branch": "ticket/T-700", "budget_sha256": None,
                    "created_at_epoch": 1, "current_stage": "FIX builder",
                    "current_state": "Review", "factory_sha": KIT_SHA,
                    "head_sha": merge_sha, "passport_factory_sha": KIT_SHA,
                    "passport_sha256": value["passport_sha256"],
                    "resume_state": None, "run_snapshot_sha256": "2" * 64,
                    "schema": "nysa.software-factory.ticket-pause/v2",
                    "status": "blocked", "ticket": "T-700",
                    "worktree": str(self.product.resolve()),
                }
                pause["pause_sha256"] = hashlib.sha256(json.dumps(
                    pause, ensure_ascii=True, sort_keys=True,
                    separators=(",", ":"),
                ).encode()).hexdigest()
                pause_path = self.controller_state / "pause-T-700.json"
                pause_path.write_text(json.dumps(pause))
                pause_path.chmod(0o600)
        issued = TICKET_ATTEST.datetime.now(TICKET_ATTEST.timezone.utc).replace(microsecond=0)
        self.emergency_request = self.temp / "emergency.json"
        self.emergency_request.write_text(json.dumps({
            "schema": "nysa.software-factory.emergency-closeout-request/v1",
            "issue": "https://github.com/acme/factory/issues/269",
            "operator_id": "owner-1",
            "reason": "Close exact merged work without synthesizing missing evidence.",
            "issued_at": issued.isoformat().replace("+00:00", "Z"),
            "expires_at": (issued + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        }))
        self.emergency_request = self.emergency_request.resolve()
        merged_state = {
            "merged": True, "merge_sha": merge_sha, "pr_head": merge_sha,
        }
        merged_state.update(state)
        self.write_state(**merged_state)

    def emergency(self, action, approval="", pr=None):
        arguments = [
            sys.executable, str(SCRIPT), "--ticket", "T-700",
            "--workdir", str(self.workdir), "--action", action,
            "--request", str(self.emergency_request),
        ]
        if action == "emergency-apply":
            arguments.extend(("--approve-hash", approval))
        if pr is not None:
            arguments.extend(("--pr", str(pr)))
        return command(*arguments, env=self.env, check=False)

    def test_emergency_closeout_requires_exact_hash_and_retries(self):
        self.prepare_emergency()
        planned = self.emergency("emergency-plan")
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(planned.stdout)
        self.assertIn(
            "approval hash does not match",
            self.emergency("emergency-apply", "0" * 64).stderr,
        )
        self.update_state(create_fail=True)
        failed = self.emergency("emergency-apply", plan["approval_sha256"])
        self.assertIn("did not create", failed.stderr)
        closeout_head = self.head_at(self.workdir)
        self.update_state(create_fail=False)
        retried = self.emergency("emergency-apply", plan["approval_sha256"])
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(self.head_at(self.workdir), closeout_head)
        self.assertEqual(
            TICKET_ATTEST.protected_terminal(
                self.workdir, "T-700", closeout_head,
            )["basis"],
            "attested-emergency-closeout",
        )

    def test_emergency_closeout_settles_historical_pre_go_orphan_before_intent(self):
        self.prepare_emergency()
        planned = self.emergency("emergency-plan")
        self.assertEqual(planned.returncode, 0, planned.stderr)
        orphan = self.product / "factory/runs/1787157118-91036.meta"
        historical_pre_go_orphan(orphan)

        applied = self.emergency(
            "emergency-apply", json.loads(planned.stdout)["approval_sha256"],
        )

        self.assertEqual(applied.returncode, 0, applied.stderr)
        values = dict(
            line.split("=", 1) for line in orphan.read_text().splitlines()
        )
        self.assertEqual(
            (values["phase"], values["accounting_state"], values["cost_basis"]),
            ("abandoned", "launch_void", "launch_void"),
        )

    def prepare_stale_emergency(self, *, bundle_v2=False):
        old_kit = ("d" if bundle_v2 else "e") * 40
        route = self.product / "factory/route-plans/T-700.json"
        route_value = json.loads(route.read_text())
        route_value["kit_sha"] = old_kit
        route.write_text(json.dumps(route_value, indent=2, sort_keys=True) + "\n")
        self.write_runs()
        for manifest in (self.product / "factory/runs").glob("*.meta"):
            manifest.write_text(manifest.read_text().replace(
                f"kit_sha={KIT_SHA}", f"kit_sha={old_kit}",
            ))
        self.commit("bind prior release evidence")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.env["FACTORY_RELEASE_SHA"] = old_kit
        if bundle_v2:
            self.add_legacy_planner()
            pinned = self.product / "factory/runs/planner-pinned-1.meta"
            pinned.write_text(pinned.read_text().replace(
                f"kit_sha={KIT_SHA}", f"kit_sha={old_kit}",
            ))
        self.bundle()
        stale_bundle = json.loads(
            (self.product / "factory/attestations/T-700/bundle.json").read_text()
        )
        stale_bundle_commit = command(
            "git", "log", "-1", "--format=%H", "HEAD", "--",
            "factory/attestations/T-700/bundle.json", cwd=self.product,
        ).stdout.strip()
        self.assertEqual(stale_bundle["kit_sha"], old_kit)

        route_value["kit_sha"] = KIT_SHA
        route.write_text(json.dumps(route_value, indent=2, sort_keys=True) + "\n")
        ticket = self.product / "factory/tickets/T-700.md"
        ticket.write_text(ticket.read_text().replace(
            "State: Awaiting Approval", "State: Review",
        ))
        self.commit("migrate release while preserving stale bundle")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.env["FACTORY_RELEASE_SHA"] = KIT_SHA
        self.prepare_emergency()
        self.assertIn(
            "lacks exact authenticated successor lineage",
            self.emergency("emergency-plan").stderr,
        )
        passport_path = self.controller_state / "passports/T-700.json"
        passport = json.loads(passport_path.read_text())
        passport.pop("authentication_sha256")
        passport.pop("passport_sha256")
        passport["factory_release_history"].insert(0, {
            "contract_version": "1.8.0", "factory_sha": old_kit,
        })
        prior_passport_file = "6" * 64
        prior_passport = "7" * 64
        current_route = hashlib.sha256(
            command(
                "git", "show", f"{passport['head_sha']}:factory/route-plans/T-700.json",
                cwd=self.workdir,
            ).stdout.encode()
        ).hexdigest()
        passport.update({
            "protected_base_sha": passport["head_sha"],
            "route_plan_sha256": current_route,
            "parent_file_sha256": prior_passport_file,
            "parent_digest": prior_passport,
            "migration_history": [{
                "schema": "nysa.software-factory.ticket-passport-migration/v2",
                "from_factory_sha": old_kit,
                "from_head_sha": stale_bundle_commit,
                "from_passport_file_sha256": prior_passport_file,
                "from_passport_sha256": prior_passport,
                "from_protected_base_sha": stale_bundle["branch_head"],
                "from_route_plan_sha256": stale_bundle["route_plan_sha256"],
                "to_factory_sha": KIT_SHA,
                "to_head_sha": passport["head_sha"],
                "to_protected_base_sha": passport["head_sha"],
                "to_route_plan_sha256": current_route,
            }],
        })
        canonical = lambda item: (json.dumps(
            item, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ) + "\n").encode()
        passport["authentication_sha256"] = hmac.new(
            b"p" * 32, canonical(passport), hashlib.sha256,
        ).hexdigest()
        passport["passport_sha256"] = hashlib.sha256(
            canonical(passport)
        ).hexdigest()
        passport_path.write_bytes(canonical(passport))
        passport_path.chmod(0o600)

        planned = self.emergency("emergency-plan")
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(planned.stdout)
        self.assertEqual(
            plan["plan"]["schema"],
            "nysa.software-factory.emergency-closeout-plan/v2",
        )
        stale = plan["plan"]["stale_attestation"]
        self.assertEqual(stale["kit_sha"], old_kit)
        self.assertEqual(
            stale["path"], "factory/attestations/T-700/bundle.json",
        )
        return old_kit, plan

    def test_emergency_closeout_accepts_exact_prior_kit_bundle_v2(self):
        _, plan = self.prepare_stale_emergency(bundle_v2=True)
        bundle = json.loads(command(
            "git", "show",
            f"{plan['plan']['protected_main']['commit']}:"
            "factory/attestations/T-700/bundle.json",
            cwd=self.workdir,
        ).stdout)
        self.assertEqual(bundle["schema"], "nysa.software-factory.ticket-bundle/v2")

    def test_emergency_closeout_retires_exact_prior_kit_bundle_and_retries(self):
        _, plan = self.prepare_stale_emergency()
        self.update_state(create_fail=True)
        failed = self.emergency("emergency-apply", plan["approval_sha256"])
        self.assertIn("did not create", failed.stderr)
        closeout_head = self.head_at(self.workdir)
        self.assertFalse(
            (self.workdir / "factory/attestations/T-700/bundle.json").exists()
        )
        self.update_state(create_fail=False)
        retried = self.emergency("emergency-apply", plan["approval_sha256"])
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(self.head_at(self.workdir), closeout_head)
        self.assertEqual(
            TICKET_ATTEST.protected_terminal(
                self.workdir, "T-700", closeout_head,
            )["basis"],
            "attested-emergency-closeout",
        )

    def test_emergency_closeout_requires_exact_authenticated_bundle_lineage(self):
        _, _ = self.prepare_stale_emergency()
        passport_path = self.controller_state / "passports/T-700.json"
        original = json.loads(passport_path.read_text())

        def write_passport(value):
            value.pop("authentication_sha256", None)
            value.pop("passport_sha256", None)
            canonical = lambda item: (json.dumps(
                item, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
            ) + "\n").encode()
            value["authentication_sha256"] = hmac.new(
                b"p" * 32, canonical(value), hashlib.sha256,
            ).hexdigest()
            value["passport_sha256"] = hashlib.sha256(canonical(value)).hexdigest()
            passport_path.write_bytes(canonical(value))
            passport_path.chmod(0o600)

        variants = {}
        foreign = json.loads(json.dumps(original))
        foreign["migration_history"][0]["from_factory_sha"] = "f" * 40
        variants["foreign source release"] = foreign
        wrong_route = json.loads(json.dumps(original))
        wrong_route["migration_history"][0]["from_route_plan_sha256"] = "0" * 64
        variants["wrong source route"] = wrong_route
        malformed = json.loads(json.dumps(original))
        malformed["migration_history"][0].pop("to_route_plan_sha256")
        variants["malformed migration"] = malformed
        ambiguous = json.loads(json.dumps(original))
        ambiguous["migration_history"].append(
            dict(ambiguous["migration_history"][0])
        )
        variants["ambiguous migration"] = ambiguous

        for label, value in variants.items():
            with self.subTest(label=label):
                write_passport(value)
                refused = self.emergency("emergency-plan")
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn(
                    "lacks exact authenticated successor lineage", refused.stderr,
                )
        write_passport(original)

    def assert_emergency_mutation_crash_recovers(self, point):
        _, plan = self.prepare_stale_emergency()
        self.env["FACTORY_TEST_EMERGENCY_CRASH_AFTER"] = point
        crashed = self.emergency("emergency-apply", plan["approval_sha256"])
        self.assertEqual(crashed.returncode, 91, crashed.stderr)
        journal = self.controller_state / "emergency-closeout/T-700.json"
        self.assertTrue(journal.is_file())
        self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
        self.env.pop("FACTORY_TEST_EMERGENCY_CRASH_AFTER")
        retried = self.emergency("emergency-apply", plan["approval_sha256"])
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertFalse(journal.exists())
        self.assertEqual(
            TICKET_ATTEST.protected_terminal(
                self.workdir, "T-700", self.head_at(self.workdir),
            )["basis"],
            "attested-emergency-closeout",
        )

    def test_emergency_closeout_recovers_crash_after_ledger_projection(self):
        self.assert_emergency_mutation_crash_recovers("ledger")

    def test_emergency_closeout_recovers_crash_after_ticket_rewrite(self):
        self.assert_emergency_mutation_crash_recovers("ticket")

    def test_emergency_closeout_recovers_crash_after_bundle_unlink(self):
        self.assert_emergency_mutation_crash_recovers("bundle")

    def test_emergency_closeout_recovers_crash_after_done_write(self):
        self.assert_emergency_mutation_crash_recovers("done")

    def test_emergency_closeout_recovers_crash_after_commit(self):
        self.assert_emergency_mutation_crash_recovers("commit")

    def test_emergency_closeout_recovers_crash_after_push(self):
        self.assert_emergency_mutation_crash_recovers("push")

    def test_emergency_closeout_recovers_crash_before_journal_cleanup(self):
        self.assert_emergency_mutation_crash_recovers("cleanup")

    def test_emergency_closeout_recovers_push_failure_after_commit(self):
        _, plan = self.prepare_stale_emergency()
        wrong_remote = self.temp / "wrong.git"
        command("git", "init", "--bare", "-q", str(wrong_remote))
        command(
            "git", "remote", "set-url", "--push", "origin", str(wrong_remote),
            cwd=self.product,
        )
        failed = self.emergency("emergency-apply", plan["approval_sha256"])
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("configured origin", failed.stderr)
        committed = self.head_at(self.workdir)
        command(
            "git", "remote", "set-url", "--push", "origin", str(self.remote),
            cwd=self.product,
        )
        retried = self.emergency("emergency-apply", plan["approval_sha256"])
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(self.head_at(self.workdir), committed)
        observed = command(
            "git", "ls-remote", "--heads", str(self.remote),
            "refs/heads/chore/t700-closeout",
        ).stdout.split()
        self.assertEqual(observed, [committed, "refs/heads/chore/t700-closeout"])

    def assert_emergency_mutation_tamper_refused(self, point, relative):
        _, plan = self.prepare_stale_emergency()
        self.env["FACTORY_TEST_EMERGENCY_CRASH_AFTER"] = point
        crashed = self.emergency("emergency-apply", plan["approval_sha256"])
        self.assertEqual(crashed.returncode, 91, crashed.stderr)
        self.env.pop("FACTORY_TEST_EMERGENCY_CRASH_AFTER")
        target = self.workdir / relative
        target.write_text(target.read_text() + "foreign edit\n")
        refused = self.emergency("emergency-apply", plan["approval_sha256"])
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("bytes are not exact", refused.stderr)
        self.assertTrue(target.read_text().endswith("foreign edit\n"))

    def test_emergency_closeout_refuses_foreign_ledger_prefix(self):
        self.assert_emergency_mutation_tamper_refused(
            "ledger", "factory/ledger.csv",
        )

    def test_emergency_closeout_refuses_foreign_ticket_prefix(self):
        self.assert_emergency_mutation_tamper_refused(
            "ticket", "factory/tickets/T-700.md",
        )

    def test_emergency_closeout_refuses_current_kit_partial_evidence(self):
        self.bundle()
        ticket = self.product / "factory/tickets/T-700.md"
        ticket.write_text(ticket.read_text().replace(
            "State: Awaiting Approval", "State: Review",
        ))
        self.commit("strand current release bundle")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.prepare_emergency()
        refused = self.emergency("emergency-plan")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("not exact prior-kit evidence", refused.stderr)

    def test_emergency_closeout_accepts_exact_paused_checkpoint(self):
        self.prepare_emergency(paused=True)
        path = self.controller_state / "pause-T-700.json"

        def write_pause(value):
            signed = dict(value)
            signed.pop("pause_sha256", None)
            value["pause_sha256"] = hashlib.sha256(json.dumps(
                signed, ensure_ascii=True, sort_keys=True,
                separators=(",", ":"),
            ).encode()).hexdigest()
            path.write_text(json.dumps(value))
            path.chmod(0o600)

        pause = json.loads(path.read_text())
        pause["blocking_issue"] = "https://github.com/acme/factory/issues/270"
        write_pause(pause)
        self.assertIn(
            "paused emergency checkpoint is invalid",
            self.emergency("emergency-plan").stderr,
        )
        pause["blocking_issue"] = "https://github.com/acme/factory/issues/269"
        write_pause(pause)
        planned = self.emergency("emergency-plan")
        self.assertEqual(planned.returncode, 0, planned.stderr)
        blocked_passport = {
            "current_state": "Blocked-Escalated",
            "head_sha": pause["head_sha"],
            "passport_sha256": pause["passport_sha256"],
        }
        pause["current_state"] = "Blocked-Escalated"
        write_pause(pause)
        with self.assertRaisesRegex(
            TICKET_ATTEST.Refusal, "paused emergency checkpoint is invalid",
        ):
            TICKET_ATTEST.paused_claim_basis(
                path, "T-700", "ticket/T-700", "Blocked-Escalated",
                blocked_passport, "https://github.com/acme/factory/issues/269",
            )
        pause.update(current_state="Review", resume_state=None)
        write_pause(pause)
        pause["pause_sha256"] = "0" * 64
        path.write_text(json.dumps(pause))
        self.assertIn(
            "paused emergency checkpoint is invalid",
            self.emergency("emergency-plan").stderr,
        )
        write_pause(pause)
        planned = self.emergency("emergency-plan")
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(planned.stdout)
        self.assertEqual(plan["plan"]["claim"]["role"], "factory-paused")
        result = self.emergency("emergency-apply", plan["approval_sha256"])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_paused_emergency_checkpoint_resume_state_matrix(self):
        self.prepare_emergency(paused=True)
        path = self.controller_state / "pause-T-700.json"
        pause = json.loads(path.read_text())

        def validate(
            state, resume_state, passport=None, *, status="blocked", budget=None,
        ):
            pause.update(
                current_state=state, resume_state=resume_state,
                status=status, budget_sha256=budget,
            )
            signed = dict(pause)
            signed.pop("pause_sha256", None)
            pause["pause_sha256"] = hashlib.sha256(json.dumps(
                signed, ensure_ascii=True, sort_keys=True,
                separators=(",", ":"),
            ).encode()).hexdigest()
            path.write_text(json.dumps(pause))
            path.chmod(0o600)
            current = passport or {
                "current_state": state,
                "head_sha": pause["head_sha"],
                "passport_sha256": pause["passport_sha256"],
            }
            return TICKET_ATTEST.paused_claim_basis(
                path, "T-700", "ticket/T-700", state, current,
                "https://github.com/acme/factory/issues/269",
            )

        for state in (
            "Ready", "Planning", "Building", "Review",
            "Awaiting Approval", "Approved",
        ):
            with self.subTest(state=state, resume_state=None):
                self.assertEqual(validate(state, None)["role"], "factory-paused")
        self.assertEqual(
            validate("Blocked-Escalated", "Planning")["role"],
            "factory-paused",
        )
        for state, resume_state in (("Review", "Review"), ("Building", "Planning")):
            with self.subTest(state=state, resume_state=resume_state):
                self.assertEqual(
                    validate(state, resume_state)["role"], "factory-paused",
                )
        for status in ("blocked", "claimed", "waiting"):
            with self.subTest(status=status):
                self.assertEqual(
                    validate("Review", "Review", status=status)["role"],
                    "factory-paused",
                )
        self.assertEqual(
            validate(
                "Review", "Review", status="budget", budget="a" * 64,
            )["role"],
            "factory-paused",
        )
        for state, resume_state in (
            ("Blocked-Escalated", None),
            ("Blocked-Escalated", ""),
            ("Review", ""),
            ("Review", 1),
            ("Review", "Done"),
        ):
            with self.subTest(state=state, resume_state=resume_state):
                with self.assertRaisesRegex(
                    TICKET_ATTEST.Refusal,
                    "paused emergency checkpoint is invalid",
                ):
                    validate(state, resume_state)
        for status, budget in (
            ("budget", None),
            ("budget", "a" * 63),
            ("running", None),
            ("unknown", None),
        ):
            with self.subTest(status=status, budget=budget):
                with self.assertRaisesRegex(
                    TICKET_ATTEST.Refusal,
                    "paused emergency checkpoint is invalid",
                ):
                    validate("Review", "Review", status=status, budget=budget)
        for passport in (
            {
                "current_state": "Review", "head_sha": "e" * 40,
                "passport_sha256": pause["passport_sha256"],
            },
            {
                "current_state": "Review", "head_sha": pause["head_sha"],
                "passport_sha256": "e" * 64,
            },
        ):
            with self.assertRaisesRegex(
                TICKET_ATTEST.Refusal,
                "paused emergency checkpoint is invalid",
            ):
                validate("Review", None, passport)

    def test_emergency_closeout_accepts_exact_operator_built_work(self):
        ticket = self.product / "factory/tickets/T-700.md"
        ticket.write_text(ticket.read_text().replace(
            "Priority: normal\n", "Priority: normal\nAssignee: operator (built outside the software factory)\n",
        ))
        self.commit("record operator-built source")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.prepare_emergency(passport=False)
        stale_claim = self.controller_state / "claims/T-700.json"
        stale_claim.write_text("{}\n")
        stale_claim.chmod(0o600)
        self.assertIn(
            "not exact operator-built work",
            self.emergency("emergency-plan").stderr,
        )
        stale_claim.unlink()
        planned = self.emergency("emergency-plan")
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(planned.stdout)
        result = self.emergency("emergency-apply", plan["approval_sha256"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(plan["plan"]["passport"])

    def test_emergency_closeout_accepts_exact_backlog_protected_merge(self):
        ticket = self.product / "factory/tickets/T-700.md"
        ticket.write_text(ticket.read_text().replace("State: Review", "State: Backlog"))
        self.commit("record backlog split")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.prepare_emergency(
            passport=False,
            historical_head_ref="ticket/T-700-safe-implementation-reviewed",
        )
        self.assertIn(
            "nonterminal protected state",
            self.emergency("emergency-plan").stderr,
        )
        self.update_state(historical_head_ref="feature/unrelated")
        self.assertIn(
            "does not bind the protected ticket",
            self.emergency("emergency-plan", pr=7).stderr,
        )
        self.update_state(
            historical_head_ref="ticket/T-700-safe-implementation-reviewed",
        )
        planned = self.emergency("emergency-plan", pr=7)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(planned.stdout)
        self.assertEqual(
            plan["plan"]["execution_basis"], "protected-merge-no-runtime",
        )
        result = self.emergency(
            "emergency-apply", plan["approval_sha256"], pr=7,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            TICKET_ATTEST.protected_terminal(
                self.workdir, "T-700", self.head_at(self.workdir),
            )["basis"],
            "attested-emergency-closeout",
        )

    def prepare_done_after_successor_route(self):
        self.bundle()
        self.approval_overlay()
        phase_one = self.attest("approval", attest_only=True)
        self.assertEqual(phase_one.returncode, 0, phase_one.stderr)
        self.project_approval_overlay()
        self.append_successor_route()
        phase_two = self.attest("approval")
        self.assertEqual(phase_two.returncode, 0, phase_two.stderr)
        merge_sha = self.head()
        command("git", "branch", "-f", "main", merge_sha, cwd=self.product)
        command(
            "git", "push", "-q", "origin", f"{merge_sha}:refs/heads/main",
            cwd=self.product,
        )
        self.workdir = self.temp / "successor-closeout"
        command(
            "git", "worktree", "add", "-q", "-b", "chore/t700-closeout",
            str(self.workdir), "origin/main", cwd=self.product,
        )
        command(
            "git", "push", "-q", "-u", "origin", "chore/t700-closeout",
            cwd=self.workdir,
        )
        self.env["FAKE_WORKDIR"] = str(self.workdir)
        self.write_state(
            merged=True, merge_sha=merge_sha, pr_head=merge_sha,
        )

    def test_done_refuses_failed_checks_and_merge_not_on_main(self):
        self.prepare_done(checks={"ci": True, "deploy-production": False})
        self.assertIn("unsuccessful: deploy-production", self.attest("done").stderr)
        self.write_state(merged=True, merge_sha=self.head(), checks={"ci": True})
        self.assertIn("missing: deploy-production", self.attest("done").stderr)
        self.write_state(merged=True, merge_sha="d" * 40)
        self.assertIn("not reachable", self.attest("done").stderr)

    def test_done_reports_pending_post_merge_check(self):
        self.prepare_done(
            checks={"deploy-production": True},
            check_runs={"ci": [{
                "name": "ci", "status": "in_progress", "conclusion": None,
            }]},
        )
        self.assertIn("pending: ci", self.attest("done").stderr)

    def test_done_happy_path_projects_ledger_and_attests(self):
        self.prepare_done()
        result = self.attest("done")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("State: Done", (self.workdir / "factory/tickets/T-700.md").read_text())
        self.assertTrue((self.workdir / "factory/attestations/T-700/done.json").is_file())
        state = json.loads(self.state.read_text())
        self.assertEqual(state["closeout_pr"], "open")
        self.assertIn("--squash", state["closeout_merge_argv"])
        self.assertEqual(
            state["create_argv"][state["create_argv"].index("--title") + 1],
            "T-700: record protected merge closeout",
        )
        self.assertEqual(
            state["create_argv"][state["create_argv"].index("--body") + 1],
            "Factory-owned metadata and accounting closeout for T-700.\n\n"
            "No additional business approval is required. Protected checks, "
            "reviews, and merge policy remain authoritative.",
        )

    def test_done_binds_approved_pr_amid_historical_branch_prs(self):
        self.prepare_done(duplicate=True)

        result = self.attest("done")

        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(
            (self.workdir / "factory/attestations/T-700/done.json").read_text()
        )
        self.assertEqual(receipt["pr_number"], 7)

    def test_done_accepts_approval_followed_by_successor_route(self):
        self.prepare_done_after_successor_route()

        result = self.attest("done")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "State: Done",
            (self.workdir / "factory/tickets/T-700.md").read_text(),
        )

    def test_done_accepts_an_unchanged_preprojected_ledger(self):
        self.prepare_done()
        command(
            sys.executable, str(ROOT / "scripts/ledger-view.py"), "project",
            "--factory-root", str(self.product), "--workdir", str(self.workdir),
            "--ticket", "T-700", env=self.env,
        )
        command("git", "add", "factory/ledger.csv", cwd=self.workdir)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "preproject concurrent ledger", cwd=self.workdir,
        )
        command(
            "git", "push", "-q", "origin", "HEAD:main", "HEAD:chore/t700-closeout",
            cwd=self.workdir,
        )

        result = self.attest("done")
        self.assertEqual(result.returncode, 0, result.stderr)
        changed = command(
            "git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD",
            cwd=self.workdir,
        ).stdout.splitlines()
        self.assertNotIn("factory/ledger.csv", changed)

    def test_done_accepts_evidence_from_the_ticket_pinned_release(self):
        ticket = self.product / "factory/tickets/T-700.md"
        ticket.write_text(ticket.read_text().replace(
            "Priority: normal\n", f"Priority: normal\nKit-SHA: {KIT_SHA}\n",
        ))
        self.commit("pin ticket release")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.prepare_done()
        self.env["FACTORY_RELEASE_SHA"] = "b" * 40

        result = self.attest("done")
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(
            (self.workdir / "factory/attestations/T-700/done.json").read_text()
        )
        self.assertEqual(receipt["kit_sha"], KIT_SHA)

    def test_done_retries_create_failure_without_new_commit_or_projection(self):
        self.prepare_done(create_fail=True)
        failed = self.attest("done")
        self.assertIn("did not create", failed.stderr)
        closeout_head = self.head_at(self.workdir)
        count = command(
            "git", "rev-list", "--count", "HEAD", cwd=self.workdir,
        ).stdout.strip()
        ledger = (self.workdir / "factory/ledger.csv").read_bytes()
        self.update_state(create_fail=False)
        retried = self.attest("done")
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(self.head_at(self.workdir), closeout_head)
        self.assertEqual(
            command("git", "rev-list", "--count", "HEAD", cwd=self.workdir).stdout.strip(),
            count,
        )
        self.assertEqual((self.workdir / "factory/ledger.csv").read_bytes(), ledger)

    def test_done_retries_failed_closeout_push_without_new_commit(self):
        self.prepare_done()
        hook = self.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        failed = self.attest("done")
        self.assertIn("remote did not confirm", failed.stderr)
        closeout_head = self.head_at(self.workdir)
        hook.unlink()
        retried = self.attest("done")
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(self.head_at(self.workdir), closeout_head)

    def test_done_retries_auto_merge_failure_on_existing_pr(self):
        self.prepare_done(closeout_merge_fail=True)
        failed = self.attest("done")
        self.assertIn("auto-merge", failed.stderr)
        closeout_head = self.head_at(self.workdir)
        self.update_state(closeout_merge_fail=False)
        retried = self.attest("done")
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(self.head_at(self.workdir), closeout_head)
        self.assertEqual(json.loads(self.state.read_text())["create_count"], 1)

    def test_done_retries_unconfirmed_closeout_auto_merge(self):
        self.prepare_done(closeout_auto_merge=False)
        self.assertIn("did not confirm", self.attest("done").stderr)
        closeout_head = self.head_at(self.workdir)
        self.update_state(closeout_auto_merge=True)
        retried = self.attest("done")
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(self.head_at(self.workdir), closeout_head)

    def test_done_regenerates_exact_closeout_when_protected_main_advances(self):
        self.prepare_done(create_fail=True)
        self.assertIn("did not create", self.attest("done").stderr)
        stale_head = self.head_at(self.workdir)
        updater = self.temp / "concurrent-closeout-main-update"
        command(
            "git", "clone", "-q", "--branch", "main", str(self.remote),
            str(updater),
        )
        (updater / "concurrent.txt").write_text("sibling closeout\n")
        command("git", "add", ".", cwd=updater)
        command(
            "git", "-c", "user.name=test", "-c",
            "user.email=test@example.com", "commit", "-qm",
            "merge sibling closeout", cwd=updater,
        )
        command("git", "push", "-q", "origin", "main", cwd=updater)
        command("git", "fetch", "-q", "origin", "main", cwd=self.workdir)
        protected = command(
            "git", "rev-parse", "origin/main", cwd=self.workdir,
        ).stdout.strip()
        self.update_state(
            create_fail=False, closeout_merge_state="BEHIND",
        )

        retried = self.attest("done")

        self.assertEqual(retried.returncode, 0, retried.stderr)
        result = json.loads(retried.stdout)
        state = json.loads(self.state.read_text())
        receipt = json.loads(
            (self.workdir / "factory/attestations/T-700/done.json").read_text()
        )
        self.assertEqual(result["retired_closeout_pr_number"], 14)
        self.assertEqual(state["create_count"], 2)
        self.assertEqual(state["closeout_close_argv"][:3], ["pr", "close", "14"])
        self.assertNotEqual(self.head_at(self.workdir), stale_head)
        self.assertEqual(receipt["closeout_parent"], protected)

    def test_done_retry_refuses_modified_closeout_head(self):
        self.prepare_done(create_fail=True)
        self.assertNotEqual(self.attest("done").returncode, 0)
        (self.workdir / "unrelated-closeout.txt").write_text("tamper\n")
        command("git", "add", ".", cwd=self.workdir)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "modify closeout head", cwd=self.workdir,
        )
        command(
            "git", "push", "-q", "origin", "HEAD:chore/t700-closeout",
            cwd=self.workdir,
        )
        self.update_state(create_fail=False)
        self.assertIn("existing closeout commit", self.attest("done").stderr)

    def test_done_refuses_duplicate_or_wrong_closeout_pr(self):
        self.prepare_done(closeout_pr="open", closeout_duplicate=True)
        self.assertIn("exactly one closeout PR", self.attest("done").stderr)

    def test_done_refuses_wrong_closeout_pr_identity(self):
        self.prepare_done(closeout_pr="open", closeout_wrong=True)
        self.assertIn("branch, base, or head", self.attest("done").stderr)

    def test_done_refuses_merged_closeout_absent_from_protected_main(self):
        self.prepare_done(closeout_pr="merged")
        result = self.attest("done")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("protected terminal validation failed", result.stderr)
        self.assertNotIn("closeout_merge_argv", json.loads(self.state.read_text()))
        project = self.product / "factory/PROJECT.env"
        project.write_text(project.read_text() + "MAX_CONCURRENT_TICKETS=4\n")
        lease = "3" * 64
        lease_dir = self.product / "factory/.dispatch-leases"
        lease_dir.mkdir()
        lease_file = lease_dir / "T-700.json"
        lease_file.write_text(json.dumps({
            "schema_version": 1, "ticket": "T-700", "lease_id": lease,
            "expires_epoch": 4102444800,
        }))
        terminal_env = dict(os.environ)
        terminal_env.update({
            "FACTORY_ROOT": str(self.product),
            "FACTORY_CONTRACT_VERSION": "1.3.0",
        })
        pending = command(
            "bash", str(ROOT / "scripts/next-stage.sh"), "--ticket", "T-700",
            "--lease", lease, "--workdir", str(self.product),
            env=terminal_env, check=False,
        )
        self.assertEqual(pending.returncode, 0, pending.stderr)
        self.assertTrue(pending.stdout.startswith("AWAIT-MERGE "), pending.stdout)
        self.assertTrue(lease_file.exists())
        command("git", "push", "-q", "origin", "HEAD:main", cwd=self.workdir)
        command("git", "fetch", "-q", "origin", "main", cwd=self.workdir)
        stage = command(
            "bash", str(ROOT / "scripts/next-stage.sh"), "--ticket", "T-700",
            "--lease", lease, "--workdir", str(self.product),
            env=terminal_env, check=False,
        )
        self.assertEqual(stage.returncode, 0, stage.stderr)
        self.assertTrue(stage.stdout.startswith("COMPLETE "), stage.stdout)
        released = command(
            "bash", str(ROOT / "scripts/dispatch-lease.sh"), "release",
            "--ticket", "T-700", "--lease", lease,
            env=terminal_env, check=False,
        )
        self.assertEqual(released.returncode, 0, released.stderr)
        self.assertFalse(lease_file.exists())

    def test_done_refuses_missing_or_tampered_approval_and_head_mismatch(self):
        self.prepare_done()
        approval = self.workdir / "factory/attestations/T-700/approval.json"
        approval.unlink()
        command("git", "add", "-A", cwd=self.workdir)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "manual approved without receipt", cwd=self.workdir,
        )
        command(
            "git", "push", "-q", "origin", "HEAD:main",
            "HEAD:chore/t700-closeout", cwd=self.workdir,
        )
        self.assertIn("lacks bundle or approval", self.attest("done").stderr)

    def test_done_refuses_tampered_protected_approval_receipt(self):
        self.prepare_done()
        approval = self.workdir / "factory/attestations/T-700/approval.json"
        value = json.loads(approval.read_text())
        value["reviewed_sha"] = "9" * 40
        approval.write_text(json.dumps(value, sort_keys=True) + "\n")
        command("git", "add", approval, cwd=self.workdir)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "tamper protected approval", cwd=self.workdir,
        )
        command(
            "git", "push", "-q", "origin", "HEAD:main",
            "HEAD:chore/t700-closeout", cwd=self.workdir,
        )
        self.assertIn("protected approval evidence", self.attest("done").stderr)

    def test_done_refuses_merged_head_mismatch_and_check_name_collision(self):
        self.prepare_done(pr_head=self.reviewed)
        self.assertNotEqual(self.attest("done").returncode, 0)
        approval_head = command(
            "git", "rev-parse", "origin/main", cwd=self.workdir,
        ).stdout.strip()
        self.write_state(
            merged=True, merge_sha=approval_head, pr_head=approval_head,
            check_runs={"ci": [{
                "name": "ci", "status": "completed", "conclusion": "success",
            }]},
        )
        self.assertIn("ambiguous", self.attest("done").stderr)
        duplicate = {
            "name": "ci", "status": "completed", "conclusion": "success",
        }
        self.write_state(
            merged=True, merge_sha=approval_head, pr_head=approval_head,
            checks={"deploy-production": True},
            check_runs={"ci": [duplicate, dict(duplicate)]},
        )
        self.assertIn("multiple latest", self.attest("done").stderr)

    def test_done_refuses_closeout_commit_before_projection(self):
        self.prepare_done()
        (self.workdir / "arbitrary.txt").write_text("not closeout evidence\n")
        command("git", "add", ".", cwd=self.workdir)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "arbitrary closeout commit", cwd=self.workdir,
        )
        self.assertIn("certified remote tip", self.attest("done").stderr)

    def test_dispatch_lease_wrapper_requires_matching_opaque_lease_at_two(self):
        project = self.product / "factory/PROJECT.env"
        wrapper = ROOT / "scripts/ticket-attest.sh"
        env = dict(self.env)
        env.pop("FACTORY_DISPATCH_LEASE_ID", None)
        single = command(
            "bash", str(wrapper), "--ticket", "T-700", "--workdir",
            str(self.product), "--action", "invalid", env=env, check=False,
        )
        self.assertNotIn("dispatcher lease", single.stderr)
        project.write_text(project.read_text() + "MAX_CONCURRENT_TICKETS=2\n")
        lease = "1" * 64
        lease_dir = self.product / "factory/.dispatch-leases"
        lease_dir.mkdir()
        (lease_dir / "T-700.json").write_text(json.dumps({
            "schema_version": 1, "ticket": "T-700", "lease_id": lease,
            "expires_epoch": 4102444800,
        }))
        missing = command(
            "bash", str(wrapper), "--ticket", "T-700", "--workdir",
            str(self.product), "--action", "bundle", env=env, check=False,
        )
        self.assertIn("canonical dispatcher lease", missing.stderr)
        env["FACTORY_DISPATCH_LEASE_ID"] = "2" * 64
        wrong = command(
            "bash", str(wrapper), "--ticket", "T-700", "--workdir",
            str(self.product), "--action", "bundle", env=env, check=False,
        )
        self.assertIn("does not match", wrong.stderr)
        env["FACTORY_DISPATCH_LEASE_ID"] = lease
        matching = command(
            "bash", str(wrapper), "--ticket", "T-700", "--workdir",
            str(self.product), "--action", "bundle", env=env, check=False,
        )
        self.assertNotIn("dispatcher lease", matching.stderr)
        self.assertNotIn(lease, missing.stdout + missing.stderr + wrong.stdout + wrong.stderr)


if __name__ == "__main__":
    unittest.main()
