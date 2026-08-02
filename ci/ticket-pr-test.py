#!/usr/bin/env python3
"""Idempotent early ticket PR boundary test."""

import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "ticket-pr.py"
MANAGER_PATH = ROOT / "scripts" / "model-manager.py"
ROUTER_PATH = ROOT / "scripts" / "model-router.py"
ROUTER_SPEC = importlib.util.spec_from_file_location("ticket_pr_model_router", ROUTER_PATH)
ROUTER = importlib.util.module_from_spec(ROUTER_SPEC)
ROUTER_SPEC.loader.exec_module(ROUTER)
HELPER_SPEC = importlib.util.spec_from_file_location("ticket_pr_helper", HELPER)
TICKET_PR = importlib.util.module_from_spec(HELPER_SPEC)
HELPER_SPEC.loader.exec_module(TICKET_PR)
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
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def png_with_text(value):
    payload = f"case\0{value}".encode()
    kind = b"tEXt"
    chunk = (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )
    return PNG[:-12] + chunk + PNG[-12:]


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
            "MAX_CONCURRENT_TICKETS=4\nAUTO_MERGE_METHOD=squash\n"
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
        (factory / "QUALIFICATION.json").write_text('{"generation":1}\n')
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
elif args[:2] == ['pr', 'view']:
    print(json.dumps({'comments': [{
        'author': {'login': 'railway-app'},
        'body': '| api | success | [Web](https://api-example-pr-7.up.railway.app) | now |\\n'
                '| web | success | [Web](https://web-example-pr-7.up.railway.app) | now |',
    }]}))
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

    def test_exact_remote_head_retries_once(self):
        failed = subprocess.CompletedProcess(
            ["git"], 128, stdout="", stderr="transport unavailable"
        )
        passed = subprocess.CompletedProcess(
            ["git"], 0, stdout="a" * 40 + "\trefs/heads/ticket/T-100\n", stderr=""
        )
        with patch.object(TICKET_PR.subprocess, "run", side_effect=[failed, passed]) as run:
            self.assertEqual(
                TICKET_PR.git(self.product, "ls-remote", "origin"),
                "a" * 40 + "\trefs/heads/ticket/T-100",
            )
            self.assertEqual(run.call_count, 2)
        with patch.object(TICKET_PR.subprocess, "run", side_effect=[failed, failed]) as run:
            with self.assertRaises(TICKET_PR.Refusal):
                TICKET_PR.git(self.product, "ls-remote", "origin")
            self.assertEqual(run.call_count, 2)

    def command(
        self, expected=0, bucket="pass", lease_id=LEASE_ID,
        contract="", stage="", receipt="",
    ):
        head = subprocess.run(
            ["git", "-C", self.product, "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        environment = {
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
        }
        if contract:
            environment.update({
                "FACTORY_RELEASE_CONTRACT_VERSION": contract,
                "FACTORY_TRANSITION_STAGE": stage,
                "FACTORY_TRANSITION_RECEIPT_SHA256": receipt,
            })
        result = subprocess.run(
            [sys.executable, HELPER, "--ticket", "T-100", "--workdir", self.product],
            text=True, capture_output=True, check=False,
            env=environment,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def prepare_control_refresh(self):
        old_head = subprocess.run(
            ["git", "-C", self.product, "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        base = self.root / "base"
        subprocess.run(
            ["git", "-C", self.product, "worktree", "add", "-q", base, "main"],
            check=True,
        )
        target = "e" * 40
        (base / "factory/KIT_PIN").write_text(target + "\n")
        (base / "factory/QUALIFICATION.json").write_text('{"generation":2}\n')
        migration = base / "factory/migrations/inflight-release" / f"{target}.json"
        migration.parent.mkdir(parents=True)
        migration.write_text("{}\n")
        subprocess.run(["git", "-C", base, "add", "."], check=True)
        subprocess.run(
            ["git", "-C", base, "commit", "-qm", "advance control metadata"],
            check=True,
        )
        subprocess.run(["git", "-C", base, "push", "-q", "origin", "main"], check=True)
        base_head = subprocess.run(
            ["git", "-C", base, "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", self.product, "merge", "--no-ff", "-m",
             "merge protected control metadata", base_head],
            text=True, capture_output=True, check=True,
        )
        merge_head = subprocess.run(
            ["git", "-C", self.product, "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        refresh = self.product / "factory/attestations/T-100/refresh.json"
        refresh.parent.mkdir(parents=True)
        refresh.write_text(json.dumps({
            "schema": "nysa.software-factory.ticket-refresh/v1",
            "ticket": "T-100",
            "generation": 1,
            "old_head": old_head,
            "base_head": base_head,
            "merge_head": merge_head,
            "prior_reviewer_runs": 1,
            "prior_approve_verdicts": 1,
            "prior_request_changes_verdicts": 0,
            "prior_narrator_runs": 0,
            "prior_bundle_blob": None,
            "prior_approval_blob": None,
            "refreshed_at": "2026-07-20T00:02:00Z",
        }, sort_keys=True) + "\n")
        subprocess.run(["git", "-C", self.product, "add", "."], check=True)
        subprocess.run(
            ["git", "-C", self.product, "commit", "-qm", "record refresh evidence"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", self.product, "push", "-q", "origin", "ticket/T-100"],
            check=True,
        )

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

    def commit_and_push(self, message):
        subprocess.run(["git", "-C", self.product, "add", "."], check=True)
        subprocess.run(
            ["git", "-C", self.product, "commit", "-qm", message], check=True,
        )
        subprocess.run(
            ["git", "-C", self.product, "push", "-q", "origin", "ticket/T-100"],
            check=True,
        )

    def prepare_post_review_evidence(self, mode="valid"):
        bundle = self.product / "factory/tickets/T-100-bundle.md"
        evidence = self.product / "factory/tickets/T-100-evidence"
        evidence.mkdir()
        (evidence / "old.png").write_bytes(PNG)
        if mode == "unreferenced-deletion":
            (evidence / "orphan-old.png").write_bytes(PNG)
        bundle.write_text("![Old](T-100-evidence/old.png)\n")
        self.commit_and_push("record prior narrator evidence")
        self.prepare_narrator()

        if mode == "in-place":
            current = evidence / "old.png"
            current.write_bytes(png_with_text("updated"))
        else:
            (evidence / "old.png").unlink()
            current = evidence / "current.png"
            current.write_bytes(PNG)
            bundle.write_text("![Current](T-100-evidence/current.png)\n")
        if mode == "unreferenced":
            (evidence / "orphan.png").write_bytes(PNG)
        elif mode == "unreferenced-deletion":
            (evidence / "orphan-old.png").unlink()
        elif mode == "fake":
            current.write_bytes(b"not-a-png" * 8)
        elif mode == "fake-boundaries":
            current.write_bytes(
                TICKET_PR.PNG_SIGNATURE + b"not-png-chunks" + TICKET_PR.PNG_END
            )
        elif mode == "oversized":
            current.write_bytes(
                TICKET_PR.PNG_SIGNATURE
                + b"x" * TICKET_PR.MAX_NARRATOR_EVIDENCE_BYTES
                + TICKET_PR.PNG_END
            )
        elif mode == "many":
            lines = [bundle.read_text()]
            for index in range(TICKET_PR.MAX_NARRATOR_EVIDENCE_FILES):
                path = evidence / f"extra-{index}.png"
                path.write_bytes(PNG)
                lines.append(f"![Extra {index}](T-100-evidence/{path.name})\n")
            bundle.write_text("".join(lines))
        elif mode == "limit":
            lines = [bundle.read_text()]
            for index in range(TICKET_PR.MAX_NARRATOR_EVIDENCE_FILES - 2):
                path = evidence / f"extra-{index}.png"
                path.write_bytes(PNG)
                lines.append(f"![Extra {index}](T-100-evidence/{path.name})\n")
            bundle.write_text("".join(lines))
        elif mode == "executable":
            current.chmod(0o755)
        elif mode == "nested":
            nested = evidence / "nested"
            nested.mkdir()
            (nested / "current.png").write_bytes(PNG)
            bundle.write_text(
                bundle.read_text()
                + "![Nested](T-100-evidence/nested/current.png)\n"
            )
        elif mode == "symlink":
            current.unlink()
            current.symlink_to("../T-100-bundle.md")
        elif mode == "sibling":
            sibling = self.product / "factory/tickets/T-999-evidence"
            sibling.mkdir()
            (sibling / "sibling.png").write_bytes(PNG)
            bundle.write_text(
                bundle.read_text()
                + "![Sibling](T-999-evidence/sibling.png)\n"
            )
        elif mode not in {"valid", "in-place", "unreferenced-deletion"}:
            raise ValueError(mode)
        self.commit_and_push("refresh narrator evidence")

    def prepare_approval_continuation(self, mode="valid", successor=False):
        route_plan, journal = self.prepare_route_migration()
        approval_kit_sha = "d" * 40 if successor else KIT_SHA
        if successor:
            ticket_path = self.product / "factory/tickets/T-100.md"
            ticket_path.write_text(ticket_path.read_text().replace(
                f"Kit-SHA: {KIT_SHA}", f"Kit-SHA: {approval_kit_sha}", 1,
            ))
        bundle_path = self.product / "factory/tickets/T-100-bundle.md"
        bundle_path.write_text("# Evidence bundle\n\nApprove to merge?\n")
        self.commit_and_push("record narrator bundle")
        self.prepare_narrator()
        reviewed = next(
            line.partition("=")[2]
            for line in (self.product / "factory/runs/run-5.meta").read_text().splitlines()
            if line.startswith("role_head_before=")
        )
        branch_head = subprocess.run(
            ["git", "-C", self.product, "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        route_blob = subprocess.run(
            ["git", "-C", self.product, "hash-object", route_plan],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        bundle_blob = subprocess.run(
            ["git", "-C", self.product, "hash-object", bundle_path],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        ticket_path = self.product / "factory/tickets/T-100.md"
        ticket_path.write_text(ticket_path.read_text().replace(
            "State: Building", "State: Awaiting Approval", 1,
        ))
        attestation_root = self.product / "factory/attestations/T-100"
        attestation_root.mkdir(parents=True)
        bundle_receipt = {
            "schema": "nysa.software-factory.ticket-bundle/v1",
            "ticket": "T-100",
            "repository": "example/product",
            "branch": "ticket/T-100",
            "branch_head": branch_head,
            "reviewed_sha": reviewed,
            "bundle_path": "factory/tickets/T-100-bundle.md",
            "bundle_blob": bundle_blob,
            "pr_number": 7,
            "pr_url": "https://example.invalid/pr/7",
            "reviewer_run_id": "run-5",
            "narrator_run_id": "run-6",
            "kit_sha": approval_kit_sha,
            "policy_hash": "1" * 64,
            "route_plan_path": "factory/route-plans/T-100.json",
            "route_plan_blob": route_blob,
            "route_plan_sha256": hashlib.sha256(route_plan.read_bytes()).hexdigest(),
            "attested_at": "2026-07-20T01:00:00Z",
        }
        bundle_receipt_path = attestation_root / "bundle.json"
        bundle_receipt_path.write_text(
            json.dumps(bundle_receipt, indent=2, sort_keys=True) + "\n"
        )
        self.commit_and_push("attest bundle")

        parent_head = subprocess.run(
            ["git", "-C", self.product, "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        ticket = ticket_path.read_text().replace(
            "State: Awaiting Approval",
            "State: Approved\nOperator-Approval: Linear",
            1,
        )
        if mode == "ticket-drift":
            ticket += "Unreviewed approval-time scope.\n"
        ticket_path.write_text(ticket)
        bundle_attestation_blob = subprocess.run(
            ["git", "-C", self.product, "hash-object", bundle_receipt_path],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        approval_receipt = {
            "schema": "nysa.software-factory.ticket-approval/v1",
            "ticket": "T-100",
            "repository": "example/product",
            "branch": "ticket/T-100",
            "parent_head": parent_head,
            "reviewed_sha": reviewed,
            "bundle_blob": bundle_blob,
            "bundle_attestation_blob": bundle_attestation_blob,
            "pr_number": 7,
            "operator_version": "2" * 64,
            "linear_updated_at": "2026-07-20T02:00:00Z",
            "observed_at": "2026-07-20T02:01:00Z",
            "kit_sha": approval_kit_sha,
            "auto_merge_method": "squash",
            "attested_at": "2026-07-20T02:01:00Z",
        }
        if mode == "tampered-receipt":
            approval_receipt["reviewed_sha"] = "9" * 40
        approval_path = attestation_root / "approval.json"
        approval_path.write_text(
            json.dumps(approval_receipt, indent=2, sort_keys=True) + "\n"
        )
        if mode == "duplicate-key":
            approval_path.write_text(
                approval_path.read_text().replace(
                    '  "ticket": "T-100"',
                    '  "ticket": "T-100",\n  "ticket": "T-100"',
                )
            )
        elif mode == "executable":
            approval_path.chmod(0o755)
        elif mode == "extra-path":
            (self.product / "approval-side-effect.txt").write_text("not trusted\n")
        elif mode not in {"valid", "tampered-receipt", "ticket-drift"}:
            raise ValueError(mode)
        self.commit_and_push("attest Linear approval")
        return route_plan, journal, approval_kit_sha

    def publication_command(self, expected=0):
        return self.command(
            expected=expected,
            contract="1.8.0",
            stage="AWAIT-OPERATOR bundle approval",
            receipt="f" * 64,
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
        ready = self.command()
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(
            ready["preview_urls"],
            [
                "https://api-example-pr-7.up.railway.app",
                "https://web-example-pr-7.up.railway.app",
            ],
        )

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

    def test_review_evidence_uses_canonical_worktree_ledger(self):
        self.prepare_narrator(accounting_state="abandoned_conservative")
        canonical = self.root / "canonical-product"
        subprocess.run(["git", "init", "-q", "-b", "main", canonical], check=True)
        (canonical / "factory").mkdir()
        canonical_ledger = canonical / "factory/runtime-ledger.csv"
        canonical_ledger.write_bytes(self.ledger.read_bytes())
        self.ledger.write_text(HEADER + "\n")

        with patch.dict(os.environ, {"FACTORY_LEDGER": ""}):
            reviewed = TICKET_PR.latest_reviewer_head(
                self.product, canonical, "T-100",
            )
        self.assertRegex(reviewed, r"^[0-9a-f]{40}$")

    def test_narrator_recovery_accepts_current_ticket_route_metadata(self):
        route_plan, journal = self.prepare_route_migration()
        self.prepare_narrator(accounting_state="abandoned_conservative")
        self.append_route_migration(route_plan, journal)
        recovered = self.command()
        self.assertEqual(recovered["boundary"], "narrator")
        self.assertEqual(recovered["status"], "ready")

    def test_publication_accepts_referenced_current_ticket_png_evidence(self):
        self.prepare_post_review_evidence()
        ready = self.publication_command()
        self.assertEqual(ready["boundary"], "publication")
        self.assertEqual(ready["status"], "ready")

    def test_publication_accepts_referenced_in_place_png_update(self):
        self.prepare_post_review_evidence("in-place")
        ready = self.publication_command()
        self.assertEqual(ready["status"], "ready")

    def test_publication_accepts_exact_narrator_evidence_file_limit(self):
        self.prepare_post_review_evidence("limit")
        ready = self.publication_command()
        self.assertEqual(ready["status"], "ready")

    def test_publication_accepts_exact_factory_approval_continuation(self):
        self.prepare_approval_continuation()
        ready = self.publication_command()
        self.assertEqual(ready["boundary"], "publication")
        self.assertEqual(ready["status"], "ready")

    def test_publication_accepts_approval_then_successor_route_migration(self):
        route_plan, journal, prior_kit = self.prepare_approval_continuation(
            successor=True,
        )
        ticket = self.product / "factory/tickets/T-100.md"
        ticket.write_text(ticket.read_text().replace(
            f"Kit-SHA: {prior_kit}", f"Kit-SHA: {KIT_SHA}", 1,
        ))
        self.append_route_migration(route_plan, journal)
        ready = self.publication_command()
        self.assertEqual(ready["boundary"], "publication")
        self.assertEqual(ready["status"], "ready")

    def test_publication_rejects_changed_approval_after_successor_migration(self):
        route_plan, journal, prior_kit = self.prepare_approval_continuation(
            successor=True,
        )
        ticket = self.product / "factory/tickets/T-100.md"
        ticket.write_text(ticket.read_text().replace(
            f"Kit-SHA: {prior_kit}", f"Kit-SHA: {KIT_SHA}", 1,
        ))
        self.append_route_migration(route_plan, journal)
        approval = self.product / "factory/attestations/T-100/approval.json"
        approval.write_text(approval.read_text().replace(
            '"operator_version": "' + "2" * 64 + '"',
            '"operator_version": "' + "3" * 64 + '"',
        ))
        self.commit_and_push("tamper with migrated approval")
        refused = self.publication_command(expected=2)
        self.assertIn("approval continuation", refused["error"])
        self.assertFalse(self.trace.exists())

    def test_publication_rejects_ticket_drift_after_successor_migration(self):
        route_plan, journal, prior_kit = self.prepare_approval_continuation(
            successor=True,
        )
        ticket = self.product / "factory/tickets/T-100.md"
        ticket.write_text(
            ticket.read_text().replace(
                f"Kit-SHA: {prior_kit}", f"Kit-SHA: {KIT_SHA}", 1,
            ) + "Unattested successor scope.\n"
        )
        self.append_route_migration(route_plan, journal)
        refused = self.publication_command(expected=2)
        self.assertIn("approval continuation", refused["error"])
        self.assertFalse(self.trace.exists())

    def test_publication_rejects_tampered_approval_receipt(self):
        self.prepare_approval_continuation("tampered-receipt")
        refused = self.publication_command(expected=2)
        self.assertIn("approval", refused["error"])
        self.assertFalse(self.trace.exists())

    def test_publication_rejects_approval_time_ticket_drift(self):
        self.prepare_approval_continuation("ticket-drift")
        refused = self.publication_command(expected=2)
        self.assertIn("approval", refused["error"])
        self.assertFalse(self.trace.exists())

    def test_publication_rejects_unsafe_approval_receipt(self):
        self.prepare_approval_continuation("executable")
        refused = self.publication_command(expected=2)
        self.assertIn("approval", refused["error"])
        self.assertFalse(self.trace.exists())

    def test_publication_rejects_duplicate_approval_keys(self):
        self.prepare_approval_continuation("duplicate-key")
        refused = self.publication_command(expected=2)
        self.assertIn("duplicate key", refused["error"])
        self.assertFalse(self.trace.exists())

    def test_publication_rejects_approval_commit_side_effects(self):
        self.prepare_approval_continuation("extra-path")
        refused = self.publication_command(expected=2)
        self.assertIn("approval", refused["error"])
        self.assertFalse(self.trace.exists())

    def test_publication_rejects_unreferenced_narrator_evidence(self):
        self.prepare_post_review_evidence("unreferenced")
        refused = self.publication_command(expected=2)
        self.assertIn("implementation changed", refused["error"])
        self.assertFalse(self.trace.exists())

    def test_publication_rejects_fake_png_narrator_evidence(self):
        self.prepare_post_review_evidence("fake")
        refused = self.publication_command(expected=2)
        self.assertIn("implementation changed", refused["error"])
        self.assertFalse(self.trace.exists())

    def test_publication_rejects_fake_png_with_valid_boundaries(self):
        self.prepare_post_review_evidence("fake-boundaries")
        refused = self.publication_command(expected=2)
        self.assertIn("implementation changed", refused["error"])
        self.assertFalse(self.trace.exists())

    def test_publication_rejects_unreferenced_evidence_deletion(self):
        self.prepare_post_review_evidence("unreferenced-deletion")
        refused = self.publication_command(expected=2)
        self.assertIn("implementation changed", refused["error"])
        self.assertFalse(self.trace.exists())

    def test_publication_rejects_oversized_narrator_evidence(self):
        self.prepare_post_review_evidence("oversized")
        refused = self.publication_command(expected=2)
        self.assertIn("implementation changed", refused["error"])
        self.assertFalse(self.trace.exists())

    def test_publication_rejects_excess_narrator_evidence_files(self):
        self.prepare_post_review_evidence("many")
        refused = self.publication_command(expected=2)
        self.assertIn("implementation changed", refused["error"])
        self.assertFalse(self.trace.exists())

    def test_publication_rejects_symlinked_narrator_evidence(self):
        self.prepare_post_review_evidence("symlink")
        refused = self.publication_command(expected=2)
        self.assertIn("implementation changed", refused["error"])
        self.assertFalse(self.trace.exists())

    def test_publication_rejects_executable_narrator_evidence(self):
        self.prepare_post_review_evidence("executable")
        refused = self.publication_command(expected=2)
        self.assertIn("implementation changed", refused["error"])
        self.assertFalse(self.trace.exists())

    def test_publication_rejects_nested_narrator_evidence(self):
        self.prepare_post_review_evidence("nested")
        refused = self.publication_command(expected=2)
        self.assertIn("implementation changed", refused["error"])
        self.assertFalse(self.trace.exists())

    def test_publication_rejects_sibling_ticket_narrator_evidence(self):
        self.prepare_post_review_evidence("sibling")
        refused = self.publication_command(expected=2)
        self.assertIn("implementation changed", refused["error"])
        self.assertFalse(self.trace.exists())

    def test_narrator_recovery_accepts_only_authenticated_control_refresh(self):
        self.prepare_narrator(accounting_state="abandoned_conservative")
        self.prepare_control_refresh()
        recovered = self.command(
            contract="1.8.0", stage="RUN narrator", receipt="f" * 64,
        )
        self.assertEqual(recovered["boundary"], "narrator")
        self.assertEqual(recovered["status"], "ready")

        (self.product / "implementation.txt").write_text("changed after refresh\n")
        subprocess.run(["git", "-C", self.product, "add", "implementation.txt"], check=True)
        subprocess.run(
            ["git", "-C", self.product, "commit", "-qm", "late implementation"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", self.product, "push", "-q", "origin", "ticket/T-100"],
            check=True,
        )
        refused = self.command(
            expected=2, contract="1.8.0", stage="RUN narrator", receipt="f" * 64,
        )
        self.assertIn("implementation changed", refused["error"])

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
