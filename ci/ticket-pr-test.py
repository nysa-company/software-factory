#!/usr/bin/env python3
"""Idempotent early ticket PR boundary test."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "ticket-pr.py"
MANAGER_PATH = ROOT / "scripts" / "model-manager.py"
ROUTER_PATH = ROOT / "scripts" / "model-router.py"
ROUTER_SPEC = importlib.util.spec_from_file_location("ticket_pr_model_router", ROUTER_PATH)
ROUTER = importlib.util.module_from_spec(ROUTER_SPEC)
ROUTER_SPEC.loader.exec_module(ROUTER)
MANAGER_SPEC = importlib.util.spec_from_file_location("ticket_pr_model_manager", MANAGER_PATH)
MANAGER = importlib.util.module_from_spec(MANAGER_SPEC)
MANAGER_SPEC.loader.exec_module(MANAGER)
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
        self.model_state = self.root / "model-state"
        self.model_state.mkdir(mode=0o700)
        self.catalog, self.routes, _, self.profiles = ROUTER.load_policy()
        self.readiness = {
            route_id: {
                "adapter_version": "test-v1",
                "reason": "test",
                "reported_identity": route["expected_reported_identity"],
                "state": "READY",
            }
            for route_id, route in self.routes.items()
            if route["enabled"]
        }
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
    if bucket == 'unreported':
        print("no required checks reported on the 'ticket/T-100' branch", file=sys.stderr)
        raise SystemExit(1)
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
                "FACTORY_MODEL_STATE_ROOT": str(self.model_state),
                "FACTORY_PROJECT": "example-product",
                "FAKE_PR_STATE": str(self.state),
                "FAKE_PR_TRACE": str(self.trace),
                "FAKE_PR_HEAD": head,
                "FAKE_CHECK_BUCKET": bucket,
            },
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def prepare_route_migration(self):
        route_plans = self.product / "factory/route-plans"
        route_plans.mkdir()
        route_plan = route_plans / "T-100.json"
        subprocess.run(
            [
                sys.executable, MANAGER_PATH, "pin",
                "--state-root", str(self.model_state),
                "--project", "example-product",
                "--ticket", "T-100",
                "--kit-sha", "a" * 40,
                "--readiness", json.dumps(self.readiness),
                "--output", str(route_plan),
            ],
            text=True, capture_output=True, check=True,
        )
        legacy = route_plan.read_bytes()
        journal = MANAGER.migrate_v1_plan(
            legacy, "b" * 40, "c" * 40, "2026-07-20T00:00:00Z",
            self.catalog, self.routes, self.profiles,
        )
        route_plan.write_text(ROUTER.canonical_json(journal) + "\n")
        subprocess.run(["git", "-C", self.product, "add", "."], check=True)
        subprocess.run(
            ["git", "-C", self.product, "commit", "-qm", "pin prior route journal"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", self.product, "push", "-q", "origin", "ticket/T-100"],
            check=True,
        )
        return route_plan, journal

    def append_route_migration(self, route_plan, journal):
        migrated = MANAGER.migrate_v2_journal(
            journal, "d" * 40, KIT_SHA, "2026-07-20T00:01:00Z",
            self.catalog, self.routes, self.profiles, self.readiness,
        )
        route_plan.write_text(ROUTER.canonical_json(migrated) + "\n")
        subprocess.run(["git", "-C", self.product, "add", "."], check=True)
        subprocess.run(
            ["git", "-C", self.product, "commit", "-qm", "migrate route metadata"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", self.product, "push", "-q", "origin", "ticket/T-100"],
            check=True,
        )
        return migrated

    def prepare_narrator(
        self, *, late_implementation=False, accounting_state="completed",
        phase="completed", role_exit="ok",
    ):
        reviewed = subprocess.run(
            ["git", "-C", self.product, "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        runs = self.product / "factory/runs"
        runs.mkdir()
        (runs / "run-5.meta").write_text(
            f"accounting_schema=1\naccounting_state={accounting_state}\n"
            f"cost_basis={'conservative_reservation' if accounting_state == 'abandoned_conservative' else 'actual'}\n"
            f"exit_status=0\ngo_issued=1\nphase={phase}\nrole=reviewer\n"
            f"role_exit={role_exit}\nrole_head_before={reviewed}\ntask_submitted=1\n"
            "run_id=run-5\nticket=T-100\n"
        )
        self.write_ledger(("planner", "spec-linter", "test-author", "builder", "reviewer"))
        if accounting_state == "abandoned_conservative":
            self.ledger.write_text(self.ledger.read_text().replace(
                "run-5,mock,model,selected,actual,v1",
                "run-5,mock,model,selected,conservative_reservation,v1",
            ))
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
        unreported = self.command(bucket="unreported")
        self.assertEqual(unreported["status"], "wait")
        self.assertEqual(unreported["checks"], ["required checks not reported"])
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
        self.prepare_narrator(accounting_state="abandoned_conservative")
        recovered = self.command()
        self.assertEqual(recovered["boundary"], "narrator")
        self.assertEqual(recovered["status"], "ready")
        self.assertEqual(self.trace.read_text().count("pr create"), 1)

    def test_narrator_recovery_accepts_current_ticket_route_metadata(self):
        route_plan, journal = self.prepare_route_migration()
        self.prepare_narrator(accounting_state="abandoned_conservative")
        self.append_route_migration(route_plan, journal)
        recovered = self.command()
        self.assertEqual(recovered["boundary"], "narrator")
        self.assertEqual(recovered["status"], "ready")

    def test_narrator_recovery_rejects_tampered_route_migration(self):
        route_plan, journal = self.prepare_route_migration()
        self.prepare_narrator()
        migrated = self.append_route_migration(route_plan, journal)
        tampered = copy.deepcopy(migrated)
        tampered["revisions"][-1]["revision_hash"] = "0" * 64
        route_plan.write_text(ROUTER.canonical_json(tampered) + "\n")
        subprocess.run(["git", "-C", self.product, "add", "."], check=True)
        subprocess.run(
            ["git", "-C", self.product, "commit", "-qm", "tamper route metadata"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", self.product, "push", "-q", "origin", "ticket/T-100"],
            check=True,
        )
        refused = self.command(expected=2)
        self.assertIn("model-manager", refused["error"])
        self.assertFalse(self.trace.exists())

    def test_narrator_recovery_rejects_other_ticket_route_metadata(self):
        self.prepare_narrator()
        route_plans = self.product / "factory/route-plans"
        route_plans.mkdir()
        (route_plans / "T-999.json").write_text('{"ticket":"T-999"}\n')
        subprocess.run(["git", "-C", self.product, "add", "."], check=True)
        subprocess.run(
            ["git", "-C", self.product, "commit", "-qm", "alter sibling route metadata"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", self.product, "push", "-q", "origin", "ticket/T-100"],
            check=True,
        )
        refused = self.command(expected=2)
        self.assertIn("implementation changed", refused["error"])
        self.assertFalse(self.trace.exists())

    def test_invalid_narrator_lineage_never_accesses_github(self):
        self.prepare_narrator(late_implementation=True)
        refused = self.command(expected=2)
        self.assertIn("implementation changed", refused["error"])
        self.assertFalse(self.trace.exists())

    def test_mismatched_lease_is_refused_before_pr_access(self):
        refused = self.command(expected=2, lease_id="b" * 64)
        self.assertIn("lease is missing, unsafe, or does not match", refused["error"])
        self.assertFalse(self.trace.exists())

    def test_invalid_reviewer_execution_never_accesses_github(self):
        self.prepare_narrator()
        manifest = self.product / "factory/runs/run-5.meta"
        valid = manifest.read_text()
        cases = (
            ("phase=completed", "phase=running"),
            ("accounting_state=completed", "accounting_state=cancelled_conservative"),
            ("accounting_schema=1", "accounting_schema=2"),
            ("go_issued=1", "go_issued=0"),
            ("task_submitted=1", "task_submitted=0"),
            ("role_exit=ok", "role_exit=reviewer_mutated_worktree"),
            ("exit_status=0", "exit_status=1"),
        )
        for original, replacement in cases:
            with self.subTest(replacement=replacement):
                manifest.write_text(valid.replace(original, replacement))
                refused = self.command(expected=2)
                self.assertIn("reviewer manifest is missing", refused["error"])
                self.assertFalse(self.trace.exists())


if __name__ == "__main__":
    unittest.main()
