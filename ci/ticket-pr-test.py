#!/usr/bin/env python3
"""Idempotent early ticket PR boundary test."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "ticket-pr.py"
KIT_SHA = subprocess.run(
    ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
    text=True, capture_output=True, check=True,
).stdout.strip()
HEADER = (
    "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,"
    "run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version"
)
LEASE_ID = "a" * 64


class TicketPrTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", self.remote], check=True)
        self.product = self.root / "product"
        subprocess.run(["git", "init", "-q", "-b", "main", self.product], check=True)
        for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid")):
            subprocess.run(["git", "-C", self.product, "config", key, value], check=True)
        factory = self.product / "factory"
        (factory / "tickets").mkdir(parents=True)
        (factory / "PROJECT.env").write_text(
            "GH_REPO=example/product\nTICKET_BRANCH_PREFIX=ticket/\n"
            "MAX_CONCURRENT_TICKETS=4\n"
        )
        leases = factory / ".dispatch-leases"
        leases.mkdir()
        (leases / "T-100.json").write_text(json.dumps({
            "schema_version": 1,
            "ticket": "T-100",
            "lease_id": LEASE_ID,
            "claimed_epoch": 1,
            "expires_epoch": 4102444800,
        }))
        (factory / "KIT_PIN").write_text(KIT_SHA + "\n")
        (factory / "tickets/T-100.md").write_text(
            f"# T-100\n\nState: Building\nInitiative: I-1\nPriority: normal\n"
            f"Kit-SHA: {KIT_SHA}\nSPEC-LINT: PASS\n"
        )
        (self.product / ".gitignore").write_text("factory/runtime-ledger.csv\nfactory/runs/\n")
        self.ledger = factory / "runtime-ledger.csv"
        self.write_ledger(("planner", "spec-linter", "test-author", "builder"))
        subprocess.run(["git", "-C", self.product, "add", "."], check=True)
        subprocess.run(["git", "-C", self.product, "commit", "-qm", "builder output"], check=True)
        subprocess.run(["git", "-C", self.product, "remote", "add", "origin", self.remote], check=True)
        subprocess.run(["git", "-C", self.product, "push", "-q", "origin", "main"], check=True)
        subprocess.run(["git", "-C", self.product, "switch", "-qc", "ticket/T-100"], check=True)
        subprocess.run(["git", "-C", self.product, "push", "-qu", "origin", "ticket/T-100"], check=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.state = self.root / "prs.json"
        self.trace = self.root / "trace"
        (self.bin / "gh").write_text(
            """#!/usr/bin/env python3
import json, os, pathlib, sys
state = pathlib.Path(os.environ['FAKE_PR_STATE'])
trace = pathlib.Path(os.environ['FAKE_PR_TRACE'])
args = sys.argv[1:]
with trace.open('a') as handle: handle.write(' '.join(args) + '\\n')
prs = json.loads(state.read_text()) if state.exists() else []
if args[:2] == ['pr', 'list']:
    print(json.dumps(prs))
elif args[:2] == ['pr', 'create']:
    prs = [{
        'number': 7, 'headRefName': 'ticket/T-100', 'baseRefName': 'main',
        'headRefOid': os.environ['FAKE_PR_HEAD'], 'url': 'https://example.invalid/pr/7',
        'state': 'OPEN',
    }]
    state.write_text(json.dumps(prs))
elif args[:2] == ['pr', 'checks']:
    bucket = os.environ.get('FAKE_CHECK_BUCKET', 'pass')
    print(json.dumps([{'name': 'ci', 'state': bucket, 'bucket': bucket}]))
    raise SystemExit(8 if bucket == 'pending' else 1 if bucket != 'pass' else 0)
else:
    raise SystemExit(2)
"""
        )
        (self.bin / "gh").chmod(0o700)

    def tearDown(self):
        self.temp.cleanup()

    def write_ledger(self, roles):
        rows = [HEADER]
        for index, role in enumerate(roles, 1):
            rows.append(
                f"2026-07-20,00:00:0{index},T-100,{role},mock,v1,1,0.01,0,"
                f"run-{index},mock,model,selected,actual,v1"
            )
        self.ledger.write_text("\n".join(rows) + "\n")

    def command(self, expected=0, bucket="pass", lease_id=LEASE_ID):
        head = subprocess.run(
            ["git", "-C", self.product, "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        result = subprocess.run(
            [sys.executable, HELPER, "--ticket", "T-100", "--workdir", self.product],
            text=True, capture_output=True, check=False,
            env={
                **os.environ,
                "PATH": f"{self.bin}:{os.environ['PATH']}",
                "FACTORY_ROOT": str(self.product),
                "FACTORY_LEDGER": str(self.ledger),
                "FACTORY_CERTIFIED_PRODUCT_ORIGIN": str(self.remote),
                "FACTORY_DISPATCH_LEASE_ID": lease_id,
                "FAKE_PR_STATE": str(self.state),
                "FAKE_PR_TRACE": str(self.trace),
                "FAKE_PR_HEAD": head,
                "FAKE_CHECK_BUCKET": bucket,
            },
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def prepare_narrator(self, *, late_implementation=False):
        reviewed = subprocess.run(
            ["git", "-C", self.product, "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        runs = self.product / "factory/runs"
        runs.mkdir()
        (runs / "run-5.meta").write_text(
            "accounting_state=completed\nexit_status=0\nrole=reviewer\n"
            f"role_head_before={reviewed}\nrun_id=run-5\nticket=T-100\n"
        )
        self.write_ledger(("planner", "spec-linter", "test-author", "builder", "reviewer"))
        ticket = self.product / "factory/tickets/T-100.md"
        ticket.write_text(ticket.read_text() + "Reviewer round 1: APPROVE\n")
        if late_implementation:
            (self.product / "implementation.txt").write_text("changed after review\n")
        subprocess.run(["git", "-C", self.product, "add", "."], check=True)
        subprocess.run(["git", "-C", self.product, "commit", "-qm", "record review"], check=True)
        subprocess.run(
            ["git", "-C", self.product, "push", "-q", "origin", "ticket/T-100"],
            check=True,
        )

    def test_creates_once_reuses_and_requires_reviewer_stage(self):
        first = self.command()
        second = self.command()
        self.assertEqual(first["pr_number"], second["pr_number"])
        self.assertEqual(self.trace.read_text().count("pr create"), 1)
        self.write_ledger(("planner", "spec-linter", "test-author"))
        refused = self.command(expected=2)
        self.assertIn("reviewer or narrator stage", refused["error"])

    def test_required_checks_gate_reviewer_and_narrator(self):
        pending = self.command(bucket="pending")
        self.assertEqual(pending["status"], "wait")
        failed = self.command(bucket="fail")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["checks"], ["ci"])
        self.assertEqual(self.command()["status"], "prepared")

        self.prepare_narrator()
        current = subprocess.run(
            ["git", "-C", self.product, "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        prs = json.loads(self.state.read_text())
        prs[0]["headRefOid"] = current
        self.state.write_text(json.dumps(prs))
        self.assertEqual(self.command()["status"], "ready")

        (self.product / "implementation.txt").write_text("changed after review\n")
        subprocess.run(["git", "-C", self.product, "add", "implementation.txt"], check=True)
        subprocess.run(["git", "-C", self.product, "commit", "-qm", "late implementation"], check=True)
        subprocess.run(
            ["git", "-C", self.product, "push", "-q", "origin", "ticket/T-100"],
            check=True,
        )
        current = subprocess.run(
            ["git", "-C", self.product, "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        prs = json.loads(self.state.read_text())
        prs[0]["headRefOid"] = current
        self.state.write_text(json.dumps(prs))
        refused = self.command(expected=2)
        self.assertIn("implementation changed", refused["error"])

    def test_narrator_recovery_creates_pr_only_after_valid_review_lineage(self):
        self.prepare_narrator()
        recovered = self.command()
        self.assertEqual(recovered["boundary"], "narrator")
        self.assertEqual(recovered["status"], "ready")
        self.assertEqual(self.trace.read_text().count("pr create"), 1)

    def test_invalid_narrator_lineage_never_accesses_github(self):
        self.prepare_narrator(late_implementation=True)
        refused = self.command(expected=2)
        self.assertIn("implementation changed", refused["error"])
        self.assertFalse(self.trace.exists())

    def test_mismatched_lease_is_refused_before_pr_access(self):
        refused = self.command(expected=2, lease_id="b" * 64)
        self.assertIn("lease is missing, unsafe, or does not match", refused["error"])
        self.assertFalse(self.trace.exists())


if __name__ == "__main__":
    unittest.main()
