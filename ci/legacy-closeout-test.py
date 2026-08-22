#!/usr/bin/env python3
"""Focused adversarial coverage for the one-time legacy closeout."""

import json
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import legacy_closeout  # noqa: E402
from legacy_closeout import ValidationError, protected_terminal  # noqa: E402

READINESS_SPEC = importlib.util.spec_from_file_location(
    "ticket_readiness", ROOT / "scripts/ticket-readiness.py"
)
assert READINESS_SPEC and READINESS_SPEC.loader
READINESS = importlib.util.module_from_spec(READINESS_SPEC)
READINESS_SPEC.loader.exec_module(READINESS)

OLD_KIT = "1" * 40
NEW_KIT = "2" * 40
CHECKS = ("app-tests", "ci", "policy", "test-immutability")


def command(*args, cwd=None, env=None, check=True):
    result = subprocess.run(
        args, cwd=cwd, env=env, capture_output=True, text=True,
    )
    if check and result.returncode:
        raise AssertionError(result.stderr)
    return result


class LegacyCloseoutTests(unittest.TestCase):
    def test_batched_object_reader_is_bounded_and_recovers(self):
        small = subprocess.run(
            ["git", "-C", str(self.repo), "hash-object", "-w", "--stdin"],
            input=b"small", capture_output=True, check=True,
        ).stdout.decode().strip()
        large = subprocess.run(
            ["git", "-C", str(self.repo), "hash-object", "-w", "--stdin"],
            input=b"x" * (legacy_closeout.MAX_GIT_OBJECT_BYTES + 1),
            capture_output=True, check=True,
        ).stdout.decode().strip()
        self.assertIsNone(legacy_closeout._git_object(self.repo, large))
        self.assertEqual(legacy_closeout._git_object(self.repo, small)[2], b"small")
        legacy_closeout._close_git_reader()
        with mock.patch.object(legacy_closeout, "GIT_OBJECT_TIMEOUT_SECONDS", 0):
            self.assertIsNone(legacy_closeout._git_object(self.repo, small))
        self.assertEqual(legacy_closeout._git_object(self.repo, small)[2], b"small")

    def test_ledger_snapshot_containment_allows_reordering_but_not_tampering(self):
        header = "date,ticket,run_id\n"
        snapshot = header + "2026-08-01,T-100,run-1\n2026-08-02,T-101,run-2\n"
        reordered = (
            header
            + "2026-07-01,T-099,run-0\n"
            + "2026-08-02,T-101,run-2\n"
            + "2026-08-01,T-100,run-1\n"
            + "2026-08-03,T-102,run-3\n"
        )
        self.assertTrue(legacy_closeout.ledger_contains_snapshot(reordered, snapshot))
        self.assertFalse(legacy_closeout.ledger_contains_snapshot(
            reordered.replace("T-100,run-1", "T-100,forged"), snapshot,
        ))
        self.assertFalse(legacy_closeout.ledger_contains_snapshot(
            reordered.replace("2026-08-01,T-100,run-1\n", ""), snapshot,
        ))
        self.assertFalse(legacy_closeout.ledger_contains_snapshot(
            reordered + "2026-08-04,T-100,run-1\n", snapshot,
        ))

    def test_ledger_snapshot_containment_preserves_legacy_empty_run_ids(self):
        header = "date,ticket,run_id\n"
        legacy = "2026-07-01,T-099,\n"
        snapshot = header + legacy + legacy + "2026-08-01,T-100,run-1\n"
        current = (
            header + "2026-08-01,T-100,run-1\n" + legacy + legacy
            + "2026-08-02,T-101,\n"
        )
        self.assertTrue(legacy_closeout.ledger_contains_snapshot(current, snapshot))
        self.assertFalse(legacy_closeout.ledger_contains_snapshot(
            current.replace(legacy, "", 1), snapshot,
        ))
        self.assertFalse(legacy_closeout.ledger_contains_snapshot(
            current.replace("T-099", "T-998", 1), snapshot,
        ))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="legacy-closeout-test.")
        root = Path(self.temp.name)
        self.repo = root / "product"
        self.remote = root / "product.git"
        self.bin = root / "bin"
        self.bin.mkdir()
        command("git", "init", "-q", "-b", "main", self.repo)
        command("git", "init", "--bare", "-q", self.remote)
        command("git", "-C", self.repo, "remote", "add", "origin", self.remote)
        (self.repo / "factory/tickets").mkdir(parents=True)
        (self.repo / "factory/PROJECT.env").write_text(
            "GH_REPO=acme/widget\nTICKET_BRANCH_PREFIX=ticket/\nTEST_PATHS=tests/\n"
        )
        (self.repo / "factory/KIT_PIN").write_text(OLD_KIT + "\n")
        (self.repo / "factory/tickets/T-013.md").write_text(
            "# T-013\n\nState: Review\nKit-SHA: " + OLD_KIT + "\n"
            "Priority: normal\nInitiative: I-1\nDepends-On: none\n"
            "Product-Decisions: frozen\nBuilder ownership: README.md only\n"
            "Fixture-Seams: tests/fixture.txt\nAuthentication-Seams: none\n"
            "Protected-Test-Conflicts: none\n"
        )
        (self.repo / "factory/initiatives").mkdir()
        (self.repo / "factory/initiatives/I-1.md").write_text("# Initiative\n")
        (self.repo / "tests").mkdir()
        (self.repo / "tests/fixture.txt").write_text("fixture\n")
        (self.repo / "README.md").write_text("fixture\n")
        (self.repo / "factory/tickets/T-013-bundle.md").write_text(
            "# T-013 evidence bundle\n\n## What this does\nSafe legacy work.\n"
        )
        header = (
            "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,"
            "exit_status,run_id\n"
        )
        (self.repo / "factory/ledger.csv").write_text(
            header
            + "2026-07-17,00:00:00,T-013,reviewer,test,1,1,1,0,review-13\n"
            + "2026-07-17,00:01:00,T-013,narrator,test,1,1,1,0,narrate-13\n"
        )
        self.commit("source basis")
        self.basis = command(
            "git", "-C", self.repo, "rev-parse", "HEAD"
        ).stdout.strip()
        self.tree = command(
            "git", "-C", self.repo, "rev-parse", "HEAD^{tree}"
        ).stdout.strip()
        command("git", "-C", self.repo, "push", "-q", "-u", "origin", "main")
        command("git", "-C", self.repo, "fetch", "-q", "origin")
        self.request = root / "request.json"
        self.cutoff = datetime.now(timezone.utc).replace(
            microsecond=0
        ).isoformat().replace("+00:00", "Z")
        self.request.write_text(json.dumps({
            "schema": "nysa.software-factory.legacy-closeout-request/v1",
            "repository": "acme/widget",
            "source_kit_sha": OLD_KIT,
            "target_kit_sha": NEW_KIT,
            "candidate_contract": "1.3.0",
            "cutoff": self.cutoff,
            "protected_main_basis": {
                "commit": self.basis,
                "tree": self.tree,
            },
            "required_checks": [
                {"name": name, "app_id": 7, "app_slug": "github-actions"}
                for name in CHECKS
            ],
            "authorization": {
                "method": "manual-protected-main-merge",
                "statement": "Operator manually approves this exact bounded batch.",
                "auto_merge": False,
                "bypass": False,
            },
            "tickets": [{
                "ticket": "T-013",
                "classification": "legacy-reviewed",
                "pr_number": 13,
                "independent_audit": {
                    "required": False,
                    "report_sha256": None,
                    "combined_test_sha256": None,
                },
            }],
        }, indent=2))
        self.make_gh()

    def tearDown(self):
        self.temp.cleanup()

    def commit(self, message):
        command("git", "-C", self.repo, "add", ".")
        command(
            "git", "-C", self.repo, "-c", "user.name=test",
            "-c", "user.email=test@example.com", "commit", "-qm", message,
        )

    def make_gh(self, check_names=CHECKS):
        checks = [{
            "name": name,
            "status": "completed",
            "conclusion": "success",
            "app": {"id": 7, "slug": "github-actions"},
        } for name in check_names]
        pr = {
            "number": 13,
            "state": "closed",
            "merged": True,
            "base": {"ref": "main"},
            "head": {"ref": "ticket/T-013", "sha": self.basis},
            "merge_commit_sha": self.basis,
            "merged_at": "2026-07-17T00:02:00Z",
            "merged_by": {"login": "operator"},
        }
        script = self.bin / "gh"
        script.write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            f"  *check-runs*) cat <<'EOF'\n{json.dumps({'total_count': len(checks), 'check_runs': checks})}\nEOF\n"
            "  ;;\n"
            "  *commits/*/status*) echo '{\"statuses\":[]}' ;;\n"
            f"  *pulls/13*) cat <<'EOF'\n{json.dumps(pr)}\nEOF\n"
            "  ;;\n"
            "  *) exit 2 ;;\n"
            "esac\n"
        )
        script.chmod(0o755)

    def generate(self):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}")
        return command(
            sys.executable, ROOT / "scripts/legacy-closeout.py",
            "--product", self.repo, "--request", self.request,
            env=env, check=False,
        )

    def publish_generated(self):
        self.commit("manual protected migration")
        command("git", "-C", self.repo, "push", "-q", "origin", "main")
        command("git", "-C", self.repo, "fetch", "-q", "origin")

    def test_generation_is_idempotent_and_distinct_from_normal_attestation(self):
        first = self.generate()
        self.assertEqual(first.returncode, 0, first.stderr)
        result = json.loads(first.stdout)
        self.assertTrue(result["changed"])
        self.assertFalse((self.repo / "factory/attestations").exists())
        ticket = (self.repo / "factory/tickets/T-013.md").read_text()
        self.assertIn("State: Done", ticket)
        self.assertIn("Operator-Approval: Migration", ticket)
        self.assertIn("Kit-SHA: " + OLD_KIT, ticket)
        authorization = json.loads(
            (self.repo / "factory/migrations/contract-1.3/authorization.json").read_text()
        )
        self.assertNotIn("route_plan", authorization)
        second = self.generate()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)["changed"], [])

    def test_protected_main_authority_survives_main_movement_and_rejects_forgery(self):
        self.assertEqual(self.generate().returncode, 0)
        with self.assertRaises(ValidationError):
            protected_terminal(self.repo, "T-013")
        self.publish_generated()
        self.assertEqual(
            protected_terminal(self.repo, "T-013")["basis"],
            "validated-legacy-closeout",
        )
        (self.repo / "README.md").write_text("later protected change\n")
        self.commit("move main without changing migration")
        command("git", "-C", self.repo, "push", "-q", "origin", "main")
        command("git", "-C", self.repo, "fetch", "-q", "origin")
        self.assertEqual(
            protected_terminal(self.repo, "T-013")["basis"],
            "validated-legacy-closeout",
        )
        path = self.repo / "factory/migrations/contract-1.3/T-013.json"
        value = json.loads(path.read_text())
        value["unknown"] = True
        path.write_text(json.dumps(value))
        self.commit("forge receipt")
        command("git", "-C", self.repo, "push", "-q", "origin", "main")
        command("git", "-C", self.repo, "fetch", "-q", "origin")
        with self.assertRaises(ValidationError):
            protected_terminal(self.repo, "T-013")

    def test_readiness_accepts_only_authenticated_prior_kit_terminal(self):
        self.assertEqual(self.generate().returncode, 0)
        self.publish_generated()
        READINESS.validate("T-013", self.repo)

        receipt = self.repo / "factory/migrations/contract-1.3/T-013.json"
        value = json.loads(receipt.read_text())
        value["source_ticket_blob"] = "f" * 40
        receipt.write_text(json.dumps(value) + "\n")
        self.commit("tamper historical terminal receipt")
        command("git", "-C", self.repo, "push", "-q", "origin", "main")
        command("git", "-C", self.repo, "fetch", "-q", "origin")
        with self.assertRaisesRegex(
            READINESS.ReadinessError, "protected terminal evidence is invalid",
        ):
            READINESS.validate("T-013", self.repo)

    def test_exact_reintroduction_after_protected_revert_is_allowed(self):
        self.assertEqual(self.generate().returncode, 0)
        self.publish_generated()
        migration = command(
            "git", "-C", self.repo, "rev-parse", "HEAD"
        ).stdout.strip()
        command(
            "git", "-C", self.repo, "-c", "user.name=test",
            "-c", "user.email=test@example.com", "revert", "--no-edit", migration,
        )
        command("git", "-C", self.repo, "push", "-q", "origin", "main")
        command("git", "-C", self.repo, "fetch", "-q", "origin")
        self.basis = command(
            "git", "-C", self.repo, "rev-parse", "HEAD"
        ).stdout.strip()
        self.tree = command(
            "git", "-C", self.repo, "rev-parse", "HEAD^{tree}"
        ).stdout.strip()
        self.cutoff = datetime.now(timezone.utc).replace(
            microsecond=0
        ).isoformat().replace("+00:00", "Z")
        value = json.loads(self.request.read_text())
        value["cutoff"] = self.cutoff
        value["protected_main_basis"] = {
            "commit": self.basis,
            "tree": self.tree,
        }
        self.request.write_text(json.dumps(value))
        self.make_gh()
        regenerated = self.generate()
        self.assertEqual(regenerated.returncode, 0, regenerated.stderr)
        self.publish_generated()
        self.assertEqual(
            protected_terminal(self.repo, "T-013")["basis"],
            "validated-legacy-closeout",
        )

    def test_plain_done_and_partial_or_conflicting_evidence_fail_closed(self):
        ticket = self.repo / "factory/tickets/T-013.md"
        ticket.write_text(ticket.read_text().replace("Review", "Done"))
        self.commit("forged done")
        command("git", "-C", self.repo, "push", "-q", "origin", "main")
        command("git", "-C", self.repo, "fetch", "-q", "origin")
        with self.assertRaises(ValidationError):
            protected_terminal(self.repo, "T-013")

    def test_wrong_check_app_skipped_duplicate_and_branch_advance_refuse(self):
        value = json.loads(self.request.read_text())
        value["required_checks"][0]["app_id"] = 99
        self.request.write_text(json.dumps(value))
        wrong_app = self.generate()
        self.assertNotEqual(wrong_app.returncode, 0)
        self.assertIn("wrong app", wrong_app.stderr)
        value["required_checks"][0]["app_id"] = 7
        self.request.write_text(json.dumps(value))
        command("git", "-C", self.repo, "branch", "ticket/T-013")
        (self.repo / "advance").write_text("advance\n")
        command("git", "-C", self.repo, "add", "advance")
        command(
            "git", "-C", self.repo, "-c", "user.name=test",
            "-c", "user.email=test@example.com", "commit", "-qm", "advance branch",
        )
        command(
            "git", "-C", self.repo, "push", "-q", "origin",
            "HEAD:refs/heads/ticket/T-013",
        )
        command("git", "-C", self.repo, "reset", "--hard", "-q", self.basis)
        refused = self.generate()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("advanced", refused.stderr)

    def test_out_of_band_is_limited_and_requires_both_audit_digests(self):
        value = json.loads(self.request.read_text())
        value["tickets"][0]["classification"] = "out-of-band-merged"
        value["tickets"][0]["independent_audit"] = {
            "required": True,
            "report_sha256": "a" * 64,
            "combined_test_sha256": "b" * 64,
        }
        self.request.write_text(json.dumps(value))
        result = self.generate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("T-019/T-020", result.stderr)

    def test_early_aggregate_checks_require_allowlisted_audited_review(self):
        value = json.loads(self.request.read_text())
        value["tickets"][0]["classification"] = "legacy-reviewed-aggregate"
        self.request.write_text(json.dumps(value))
        self.make_gh(("ci", "test-immutability"))
        missing_audit = self.generate()
        self.assertNotEqual(missing_audit.returncode, 0)
        self.assertIn("audit evidence", missing_audit.stderr)
        value["tickets"][0]["independent_audit"] = {
            "required": True,
            "report_sha256": "a" * 64,
            "combined_test_sha256": "b" * 64,
        }
        self.request.write_text(json.dumps(value))
        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(
            (self.repo / "factory/migrations/contract-1.3/T-013.json").read_text()
        )
        self.assertEqual(
            [item["name"] for item in receipt["checks"]],
            ["ci", "test-immutability"],
        )

    def test_batch_rejects_extra_files_partial_batch_and_normal_conflict(self):
        self.assertEqual(self.generate().returncode, 0)
        extra = self.repo / "factory/migrations/contract-1.3/extra.json"
        extra.write_text("{}\n")
        self.commit("migration with extra file")
        command("git", "-C", self.repo, "push", "-q", "origin", "main")
        command("git", "-C", self.repo, "fetch", "-q", "origin")
        with self.assertRaises(ValidationError):
            protected_terminal(self.repo, "T-013")

    def test_exact_tree_rollback_and_conflicting_normal_receipt_fail(self):
        self.assertEqual(self.generate().returncode, 0)
        self.publish_generated()
        normal = self.repo / "factory/attestations/T-013/done.json"
        normal.parent.mkdir(parents=True)
        normal.write_text('{"schema":"nysa.software-factory.ticket-done/v1"}\n')
        self.commit("conflicting partial normal receipt")
        command("git", "-C", self.repo, "push", "-q", "origin", "main")
        command("git", "-C", self.repo, "fetch", "-q", "origin")
        with self.assertRaises(ValidationError):
            protected_terminal(self.repo, "T-013")
        command("git", "-C", self.repo, "reset", "--hard", "-q", self.basis)
        command("git", "-C", self.repo, "push", "-q", "--force", "origin", "main")
        command("git", "-C", self.repo, "fetch", "-q", "origin")
        with self.assertRaises(ValidationError):
            protected_terminal(self.repo, "T-013")

    def test_protected_terminal_caches_batches_per_exact_protected_commit(self):
        first = command(
            "git", "-C", self.repo, "rev-parse", "refs/remotes/origin/main"
        ).stdout.strip()
        terminals = {
            ticket: {"basis": "test", "ticket": ticket, "text": "State: Done\n"}
            for ticket in ("T-013", "T-014")
        }
        with (
            mock.patch.object(
                legacy_closeout, "_normal_terminal",
                side_effect=(None, terminals["T-014"], None),
            ) as normal,
            mock.patch.object(
                legacy_closeout, "legacy_batch", return_value=terminals
            ) as legacy,
            mock.patch(
                "terminal_backfill.terminal_backfill_batch", return_value={}
            ) as backfill,
        ):
            self.assertEqual(
                protected_terminal(self.repo, "T-013")["ticket"], "T-013"
            )
            with self.assertRaises(ValidationError):
                protected_terminal(self.repo, "T-014")
            self.assertEqual(legacy.call_count, 1)
            self.assertEqual(backfill.call_count, 1)
            self.assertEqual(normal.call_count, 2)
            self.assertEqual(legacy.call_args.args[1], first)
            self.assertEqual(normal.call_args_list[0].args[2], first)

            (self.repo / "later").write_text("protected main advanced\n")
            self.commit("advance protected main")
            command("git", "-C", self.repo, "push", "-q", "origin", "main")
            command("git", "-C", self.repo, "fetch", "-q", "origin")
            second = command(
                "git", "-C", self.repo, "rev-parse", "refs/remotes/origin/main"
            ).stdout.strip()
            self.assertNotEqual(first, second)
            self.assertEqual(
                protected_terminal(self.repo, "T-013")["ticket"], "T-013"
            )
            self.assertEqual(legacy.call_count, 2)
            self.assertEqual(backfill.call_count, 2)
            self.assertEqual(legacy.call_args.args[1], second)
            self.assertEqual(normal.call_args_list[-1].args[2], second)


if __name__ == "__main__":
    unittest.main()
