#!/usr/bin/env python3
"""Tests for the operator authority CLI (receipt + map projection + audit)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "scripts" / "operator-cli.py"
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import operator_receipt as receipts  # noqa: E402


def run_git(product: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(product), *arguments],
        text=True, capture_output=True, check=True,
    )
    return result.stdout.strip()


class OperatorCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        self.state = base / "controller"
        self.state.mkdir(mode=0o700)
        self.remote = base / "remote.git"
        run_git(base, "init", "--bare", "--quiet", str(self.remote))
        self.product = base / "product"
        (self.product / "factory" / "tickets").mkdir(parents=True)
        run_git(self.product, "init", "--quiet", "-b", "main")
        run_git(self.product, "config", "user.name", "Test")
        run_git(self.product, "config", "user.email", "test@local")
        self.write_ticket("T-1", "Backlog")
        (self.product / ".gitignore").write_text(
            "factory/operator-map.json\nfactory/.operator-map.lock\n"
            "factory/.operator-clears/\n"
        )
        run_git(self.product, "add", "-A")
        run_git(self.product, "commit", "--quiet", "-m", "seed")
        run_git(self.product, "remote", "add", "origin", str(self.remote))
        run_git(self.product, "push", "--quiet", "-u", "origin", "main")

    def write_ticket(self, ticket: str, state: str) -> None:
        path = self.product / "factory" / "tickets" / f"{ticket}.md"
        path.write_text(f"# {ticket}\n\nState: {state}\nPriority: normal\n")

    def cli(self, *arguments: str, expect: int = 0) -> dict | None:
        result = self.raw_cli(*arguments)
        self.assertEqual(
            result.returncode, expect,
            f"stdout={result.stdout} stderr={result.stderr}",
        )
        if expect:
            self.assertIn("REFUSE", result.stderr)
            return None
        return json.loads(result.stdout)

    def raw_cli(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable, "-I", str(CLI),
                "--product", str(self.product),
                "--state-dir", str(self.state),
                *arguments,
            ],
            text=True, capture_output=True,
        )

    def map_value(self) -> dict:
        path = self.product / "factory" / "operator-map.json"
        return json.loads(path.read_text())

    def commit_ticket(self, message: str) -> None:
        run_git(self.product, "add", "factory/tickets")
        run_git(self.product, "commit", "--quiet", "-m", message)
        run_git(self.product, "push", "--quiet", "origin", "main")

    def publish_ticket_state(self, state: str) -> None:
        run_git(self.product, "switch", "--quiet", "-c", "ticket/T-1")
        self.write_ticket("T-1", state)
        run_git(self.product, "add", "factory/tickets/T-1.md")
        run_git(self.product, "commit", "--quiet", "-m", state)
        run_git(
            self.product, "push", "--quiet", "-u", "origin", "ticket/T-1",
        )
        run_git(self.product, "switch", "--quiet", "main")

    def write_pending(self, action: str, receipt: dict, **operator: str) -> None:
        value = {
            "_config": None, "_sync": {}, "initiatives": {},
            "tickets": {"T-1": {
                "operator_fields_initialized": True,
                "operator": {
                    **operator,
                    "observed_at": receipt["issued_at"],
                    "receipt_sha256": receipt["receipt_sha256"],
                },
            }},
        }
        (self.product / "factory/operator-map.json").write_text(
            json.dumps(value) + "\n"
        )

    def test_ready_projects_overlay_and_issues_receipt(self) -> None:
        receipt = self.cli("ready", "--ticket", "T-1")
        self.assertEqual(receipt["action"], "ready")
        mapping = self.map_value()
        self.assertEqual(
            sorted(mapping), ["_config", "_sync", "initiatives", "tickets"],
        )
        entry = mapping["tickets"]["T-1"]
        self.assertTrue(entry["operator_fields_initialized"])
        self.assertNotIn("operator", entry)
        self.assertTrue(receipt["consumed"])
        durable = run_git(
            self.product, "show",
            "refs/remotes/origin/ticket/T-1:factory/tickets/T-1.md",
        )
        self.assertIn("State: Ready", durable)
        with self.assertRaises(receipts.OperatorReceiptError):
            receipts.verify_consume(self.state, "T-1", "ready")

    def test_ready_refused_outside_backlog(self) -> None:
        self.write_ticket("T-1", "Building")
        self.commit_ticket("building")
        self.cli("ready", "--ticket", "T-1", expect=1)

    def test_initialize_batches_exact_tickets_and_preserves_operator_fields(self) -> None:
        self.write_ticket("T-2", "Ready")
        self.commit_ticket("second ticket")
        mapping = {
            "_config": None, "_sync": {}, "initiatives": {},
            "tickets": {"T-1": {
                "operator": {"priority": "urgent"},
                "operator_fields_initialized": True,
            }},
        }
        (self.product / "factory/operator-map.json").write_text(
            json.dumps(mapping) + "\n"
        )
        result = self.cli(
            "initialize", "--ticket", "T-1", "--ticket", "T-2",
        )
        self.assertEqual(result, {"initialized": ["T-1", "T-2"], "status": "pass"})
        observed = self.map_value()
        self.assertEqual(
            observed["tickets"]["T-1"]["operator"], {"priority": "urgent"},
        )
        self.assertTrue(observed["tickets"]["T-2"]["operator_fields_initialized"])
        self.cli("initialize", "--ticket", "T-1", "--ticket", "T-1", expect=1)

    def test_cancel_projects_canceled(self) -> None:
        receipt = self.cli("cancel", "--ticket", "T-1")
        self.assertTrue(receipt["consumed"])
        self.assertNotIn("operator", self.map_value()["tickets"]["T-1"])
        durable = run_git(
            self.product, "show",
            "refs/remotes/origin/ticket/T-1:factory/tickets/T-1.md",
        )
        self.assertIn("State: Canceled", durable)

    def test_approve_requires_attestation_then_binds_blob(self) -> None:
        self.write_ticket("T-1", "Awaiting Approval")
        self.commit_ticket("awaiting approval")
        self.cli("approve", "--ticket", "T-1", expect=1)
        attest_dir = self.product / "factory" / "attestations" / "T-1"
        attest_dir.mkdir(parents=True)
        (attest_dir / "bundle.json").write_text('{"schema": "bundle"}\n')
        receipt = self.cli("approve", "--ticket", "T-1")
        blob = run_git(
            self.product, "hash-object",
            str(attest_dir / "bundle.json"),
        )
        self.assertEqual(receipt["payload"]["bundle_attestation_blob"], blob)
        operator = self.map_value()["tickets"]["T-1"]["operator"]
        self.assertEqual(operator["state"], "Approved")
        self.assertEqual(operator["approval"], "Receipt")
        self.assertEqual(operator["state_base"], "awaiting approval")
        self.assertEqual(operator["receipt_sha256"], receipt["receipt_sha256"])
        consumed = receipts.verify_consume(
            self.state, "T-1", "approve", {"bundle_attestation_blob": blob},
        )
        self.assertEqual(consumed["receipt_sha256"], receipt["receipt_sha256"])

    def test_resume_requires_blocked_and_valid_stage(self) -> None:
        self.cli("resume", "--ticket", "T-1", "--stage", "Building", expect=1)
        self.write_ticket("T-1", "Blocked-Escalated")
        self.commit_ticket("blocked")
        self.cli("resume", "--ticket", "T-1", "--stage", "NotAStage", expect=1)
        receipt = self.cli("resume", "--ticket", "T-1", "--stage", "Building")
        self.assertEqual(receipt["payload"]["resume_stage"], "Building")
        operator = self.map_value()["tickets"]["T-1"]["operator"]
        self.assertEqual(operator["state"], "Building")
        self.assertEqual(operator["state_base"], "blocked-escalated")
        self.assertEqual(operator["receipt_sha256"], receipt["receipt_sha256"])

    def test_priority_projects_name(self) -> None:
        receipt = self.cli("priority", "--ticket", "T-1", "--priority", "high")
        operator = self.map_value()["tickets"]["T-1"]["operator"]
        self.assertEqual(operator["priority"], "high")
        self.assertEqual(operator["receipt_sha256"], receipt["receipt_sha256"])

    def test_crash_after_push_before_consume_is_idempotently_recovered(self) -> None:
        receipt = receipts.issue(self.state, "T-1", "ready", {})
        self.write_pending(
            "ready", receipt, state="Ready", state_base="backlog",
        )
        self.publish_ticket_state("Ready")

        recovered = self.cli("ready", "--ticket", "T-1")

        self.assertTrue(recovered["consumed"])
        self.assertNotIn("operator", self.map_value()["tickets"]["T-1"])
        self.assertEqual(receipts.pending(self.state), [])

    def test_crash_after_consume_before_clear_reconciles_from_git(self) -> None:
        receipt = receipts.issue(self.state, "T-1", "ready", {})
        self.write_pending(
            "ready", receipt, state="Ready", state_base="backlog",
        )
        self.publish_ticket_state("Ready")
        receipts.verify_consume_exact(
            self.state, "T-1", "ready", receipt["receipt_sha256"],
        )

        recovered = self.cli("ready", "--ticket", "T-1")

        self.assertTrue(recovered["consumed"])
        self.assertNotIn("operator", self.map_value()["tickets"]["T-1"])

    def test_failed_push_leaves_replayable_receipt_and_retry_succeeds(self) -> None:
        hook = self.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        failed = self.raw_cli("ready", "--ticket", "T-1")
        self.assertNotEqual(failed.returncode, 0)
        pending = receipts.pending(self.state)
        self.assertEqual([(item["ticket"], item["action"]) for item in pending], [
            ("T-1", "ready"),
        ])
        self.assertEqual(
            self.map_value()["tickets"]["T-1"]["operator"]["state"], "Ready",
        )

        hook.unlink()
        recovered = self.cli("ready", "--ticket", "T-1")
        self.assertTrue(recovered["consumed"])
        self.assertEqual(receipts.pending(self.state), [])

    def test_concurrent_ready_and_cancel_apply_exactly_one_terminal_decision(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                lambda action: self.raw_cli(action, "--ticket", "T-1"),
                ("ready", "cancel"),
            ))
        self.assertEqual(sorted(item.returncode for item in results), [0, 1])
        durable = run_git(
            self.product, "show",
            "refs/remotes/origin/ticket/T-1:factory/tickets/T-1.md",
        )
        final = "Ready" if results[0].returncode == 0 else "Canceled"
        self.assertIn(f"State: {final}", durable)
        all_receipts = list((self.state / "operator-receipts/T-1").glob("*.json"))
        self.assertEqual(len(all_receipts), 1)
        self.assertTrue(json.loads(all_receipts[0].read_text())["consumed"])

    def test_pending_action_cannot_be_overwritten_or_forged_by_map(self) -> None:
        self.cli("priority", "--ticket", "T-1", "--priority", "high")
        self.cli("ready", "--ticket", "T-1", expect=1)
        mapping = self.map_value()
        mapping["tickets"]["T-1"]["operator"]["receipt_sha256"] = "f" * 64
        (self.product / "factory/operator-map.json").write_text(
            json.dumps(mapping) + "\n"
        )
        self.cli("priority", "--ticket", "T-1", "--priority", "high", expect=1)

    def test_fallback_approve_projects_receipt_schema(self) -> None:
        preview = "c" * 64
        receipt = self.cli(
            "fallback-approve", "--ticket", "T-1",
            "--preview-hash", preview, "--failed-run", "run-1",
            "--reason", "provider_unavailable",
        )
        entry = self.map_value()["tickets"]["T-1"]
        approval = entry["model_fallback_approval"]
        self.assertEqual(approval["schema"], "model-fallback-receipt-approval/v1")
        self.assertEqual(approval["approval_hash"], preview)
        self.assertEqual(approval["receipt_sha256"], receipt["receipt_sha256"])
        self.assertEqual(approval["failed_run_id"], "run-1")
        consumed = receipts.verify_consume(
            self.state, "T-1", "fallback", {"preview_sha256": preview},
        )
        self.assertEqual(consumed["receipt_sha256"], receipt["receipt_sha256"])

    def test_audit_copy_committed_without_nonce(self) -> None:
        receipt = self.cli("ready", "--ticket", "T-1")
        value = json.loads(run_git(
            self.product, "show",
            "refs/remotes/origin/ticket/T-1:factory/receipts/T-1/ready-1.json",
        ))
        self.assertNotIn("nonce", value)
        self.assertEqual(value["audit"], "no-authority")
        self.assertEqual(value["receipt_sha256"], receipt["receipt_sha256"])
        subjects = run_git(
            self.product, "log", "--format=%s", "refs/remotes/origin/ticket/T-1",
        )
        self.assertIn("T-1: operator ready receipt 1", subjects.splitlines())
        status = run_git(self.product, "status", "--porcelain")
        self.assertEqual(status, "")

    def test_sync_stamp_written(self) -> None:
        self.cli("ready", "--ticket", "T-1")
        sync = self.map_value()["_sync"]
        self.assertIn("last_success_at", sync)
        self.assertEqual(
            sorted(sync["selected_ticket_success_at"]), ["T-1"],
        )

    def test_pending_lists_actionable_tickets_and_open_receipts(self) -> None:
        self.write_ticket("T-1", "Awaiting Approval")
        self.write_ticket("T-2", "Blocked-Escalated")
        self.commit_ticket("pending fixtures")
        self.cli("priority", "--ticket", "T-2", "--priority", "urgent")
        value = self.cli("pending")
        self.assertEqual(value["awaiting_approval"], ["T-1"])
        self.assertEqual(value["blocked_escalated"], ["T-2"])
        self.assertEqual(
            value["open_receipts"],
            [{
                "ticket": "T-2", "action": "priority",
                "issued_at": value["open_receipts"][0]["issued_at"],
            }],
        )

    def test_missing_ticket_refused(self) -> None:
        self.cli("ready", "--ticket", "T-404", expect=1)

    def test_init_seeds_entry_without_receipt(self) -> None:
        self.cli("init", "--ticket", "T-1")
        entry = self.map_value()["tickets"]["T-1"]
        self.assertTrue(entry["operator_fields_initialized"])
        self.assertNotIn("operator", entry)
        self.assertEqual(receipts.pending(self.state), [])


if __name__ == "__main__":
    unittest.main()
