#!/usr/bin/env python3
"""Focused adversarial coverage for the pre-contract terminal backfill."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from legacy_closeout import ValidationError, protected_terminal  # noqa: E402
from terminal_backfill import (  # noqa: E402
    AUTHORIZED_TICKETS,
    MIGRATION_DIR,
    terminal_backfill_batch,
)


OLD_KIT = "1" * 40
NEW_KIT = "2" * 40
CHECKS = ("ci", "test-immutability")


def command(*args, cwd=None, env=None, check=True):
    result = subprocess.run(
        args, cwd=cwd, env=env, capture_output=True, text=True,
    )
    if check and result.returncode:
        raise AssertionError(result.stderr)
    return result


class TerminalBackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="terminal-backfill-test.")
        root = Path(self.temp.name)
        self.repo = root / "product"
        self.remote = root / "product.git"
        self.bin = root / "bin"
        self.bin.mkdir()
        command("git", "init", "-q", "-b", "main", self.repo)
        command("git", "init", "--bare", "-q", self.remote)
        command("git", "-C", self.remote, "config", "receive.autogc", "false")
        command("git", "-C", self.repo, "remote", "add", "origin", self.remote)
        (self.repo / "factory/tickets").mkdir(parents=True)
        (self.repo / "factory/PROJECT.env").write_text(
            "GH_REPO=acme/widget\nTICKET_BRANCH_PREFIX=ticket/\n"
        )
        (self.repo / "factory/KIT_PIN").write_text(OLD_KIT + "\n")
        ledger = (
            "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,"
            "exit_status,run_id\n"
        )
        for index, ticket in enumerate(AUTHORIZED_TICKETS, 1):
            kit = f"Kit-SHA: {OLD_KIT}\n" if index >= 11 else ""
            (self.repo / f"factory/tickets/{ticket}.md").write_text(
                f"# {ticket}\n\nState: Done\n{kit}Operator-Approval: Linear\n"
            )
            if index % 2 == 0:
                (self.repo / f"factory/tickets/{ticket}-bundle.md").write_text(
                    f"# {ticket} historical bundle\n"
                )
            ledger += (
                f"2026-07-15,00:00:00,{ticket},reviewer,test,1,1,1,0,"
                f"review-{index}\n"
                f"2026-07-15,00:01:00,{ticket},narrator,test,1,1,1,0,"
                f"narrate-{index}\n"
            )
        (self.repo / "factory/ledger.csv").write_text(ledger)
        self.commit("protected source basis")
        self.basis = self.rev("HEAD")
        self.tree = self.rev("HEAD^{tree}")
        command("git", "-C", self.repo, "push", "-q", "-u", "origin", "main")
        command("git", "-C", self.repo, "fetch", "-q", "origin")
        self.cutoff = datetime.now(timezone.utc).replace(
            microsecond=0
        ).isoformat().replace("+00:00", "Z")
        self.request = root / "request.json"
        self.request.write_text(json.dumps({
            "schema": "nysa.software-factory.terminal-backfill-request/v1",
            "repository": "acme/widget",
            "target_kit_sha": NEW_KIT,
            "candidate_contract": "1.3.0",
            "cutoff": self.cutoff,
            "protected_main_basis": {
                "commit": self.basis,
                "tree": self.tree,
            },
            "authorization": {
                "method": "manual-protected-main-merge",
                "statement": "Operator manually approves this exact terminal batch.",
                "auto_merge": False,
                "bypass": False,
            },
            "tickets": [{
                "ticket": ticket,
                "implementation_pr_number": 100 + index,
                "closeout_pr_number": 200 + index,
                "required_checks": [
                    {"name": name, "app_id": 7, "app_slug": "github-actions"}
                    for name in CHECKS
                ],
            } for index, ticket in enumerate(AUTHORIZED_TICKETS, 1)],
        }, indent=2))
        self.make_gh()

    def tearDown(self):
        self.temp.cleanup()

    def rev(self, value):
        return command(
            "git", "-C", self.repo, "rev-parse", value
        ).stdout.strip()

    def commit(self, message):
        command("git", "-C", self.repo, "add", ".")
        command(
            "git", "-C", self.repo, "-c", "user.name=test",
            "-c", "user.email=test@example.com", "commit", "-qm", message,
        )

    def push(self):
        command("git", "-C", self.repo, "push", "-q", "origin", "main")
        command("git", "-C", self.repo, "fetch", "-q", "origin")

    def make_gh(
        self, *, wrong_app=False, duplicate_check=False, missing_check=False,
        failed_check=False, bad_closeout=False,
    ):
        checks = [{
            "name": name,
            "status": "completed",
            "conclusion": "failure" if failed_check and name == "ci" else "success",
            "app": {
                "id": 99 if wrong_app and name == "ci" else 7,
                "slug": "github-actions",
            },
        } for name in CHECKS]
        if missing_check:
            checks.pop()
        if duplicate_check:
            checks.append(dict(checks[0]))
        script = self.bin / "gh"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, re, sys\n"
            f"basis={self.basis!r}\n"
            f"checks={checks!r}\n"
            "arg=' '.join(sys.argv)\n"
            "if 'check-runs' in arg:\n"
            " print(json.dumps({'total_count':len(checks),'check_runs':checks}))\n"
            "elif '/status' in arg:\n"
            " print(json.dumps({'statuses':[]}))\n"
            "else:\n"
            " number=int(re.search(r'/pulls/(\\d+)',arg).group(1))\n"
            " index=number % 100\n"
            " ticket=f'T-{index:03d}'\n"
            " implementation=number < 200\n"
            f" merge=('f'*40 if ({bad_closeout!r} and not implementation) else basis)\n"
            " print(json.dumps({'number':number,'state':'closed','merged':True,"
            "'base':{'ref':'main'},'head':{'ref':"
            "(f'ticket/{ticket}-feature' if implementation else f'chore/{ticket}-closeout'),"
            "'sha':basis},'merge_commit_sha':merge,"
            "'merged_at':'2026-07-15T00:02:00Z',"
            "'merged_by':{'login':'operator'}}))\n"
        )
        script.chmod(0o755)

    def generate(self):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}")
        return command(
            sys.executable, ROOT / "scripts/terminal-backfill.py",
            "--product", self.repo, "--request", self.request,
            env=env, check=False,
        )

    def publish_generated(self):
        self.commit("manual protected terminal backfill")
        self.push()

    def mutate_request(self, callback):
        value = json.loads(self.request.read_text())
        callback(value)
        self.request.write_text(json.dumps(value, indent=2))

    def test_generation_is_exact_idempotent_and_honest_about_absent_evidence(self):
        first = self.generate()
        self.assertEqual(first.returncode, 0, first.stderr)
        result = json.loads(first.stdout)
        self.assertEqual(result["tickets"], list(AUTHORIZED_TICKETS))
        self.assertEqual(len(result["changed"]), 14)
        receipt1 = json.loads(
            (self.repo / MIGRATION_DIR / "T-001.json").read_text()
        )
        receipt2 = json.loads(
            (self.repo / MIGRATION_DIR / "T-002.json").read_text()
        )
        receipt11 = json.loads(
            (self.repo / MIGRATION_DIR / "T-011.json").read_text()
        )
        self.assertIsNone(receipt1["source_bundle_blob"])
        self.assertIsNone(receipt1["source_kit_sha"])
        self.assertIsNotNone(receipt2["source_bundle_blob"])
        self.assertEqual(receipt11["source_kit_sha"], OLD_KIT)
        self.assertEqual(receipt1["route_plan"], {"present": False, "sha256": None})
        self.assertEqual(self.generate().returncode, 0)
        self.assertEqual(json.loads(self.generate().stdout)["changed"], [])

    def test_authority_requires_protected_merge_and_survives_later_main_movement(self):
        self.assertEqual(self.generate().returncode, 0)
        with self.assertRaises(ValidationError):
            protected_terminal(self.repo, "T-001")
        self.publish_generated()
        self.assertEqual(
            protected_terminal(self.repo, "T-001")["basis"],
            "validated-terminal-backfill",
        )
        (self.repo / "README.md").write_text("later protected change\n")
        self.commit("later protected change")
        self.push()
        self.assertEqual(
            protected_terminal(self.repo, "T-012")["basis"],
            "validated-terminal-backfill",
        )

    def test_atomic_cutover_may_include_the_matching_legacy_batch(self):
        self.assertEqual(self.generate().returncode, 0)
        legacy = self.repo / "factory/migrations/contract-1.3"
        legacy.mkdir(parents=True)
        (legacy / "authorization.json").write_text(json.dumps({
            "repository": "acme/widget",
            "source_kit_sha": OLD_KIT,
            "target_kit_sha": NEW_KIT,
            "candidate_contract": "1.3.0",
            "cutoff": self.cutoff,
            "protected_main_basis": {
                "commit": self.basis,
                "tree": self.tree,
            },
            "tickets": [{
                "ticket": "T-013",
                "receipt": "factory/migrations/contract-1.3/T-013.json",
            }],
        }))
        (legacy / "T-013.json").write_text("{}\n")
        (self.repo / "factory/tickets/T-013.md").write_text("State: Done\n")
        self.publish_generated()
        self.assertEqual(
            terminal_backfill_batch(self.repo)["T-001"]["basis"],
            "validated-terminal-backfill",
        )

    def test_partial_extra_tampered_source_and_conflicting_normal_evidence_fail_closed(self):
        self.assertEqual(self.generate().returncode, 0)
        self.publish_generated()
        migration = self.rev("HEAD")
        path = self.repo / MIGRATION_DIR / "T-001.json"
        receipt = json.loads(path.read_text())
        receipt["source_bundle_blob"] = "a" * 40
        path.write_text(json.dumps(receipt))
        self.commit("forge absent bundle")
        self.push()
        with self.assertRaises(ValidationError):
            protected_terminal(self.repo, "T-001")
        command("git", "-C", self.repo, "reset", "--hard", "-q", migration)
        path.unlink()
        self.commit("remove one terminal receipt")
        command("git", "-C", self.repo, "push", "-q", "--force", "origin", "main")
        command("git", "-C", self.repo, "fetch", "-q", "origin")
        with self.assertRaises(ValidationError):
            protected_terminal(self.repo, "T-001")
        command("git", "-C", self.repo, "reset", "--hard", "-q", migration)
        ticket = self.repo / "factory/tickets/T-001.md"
        ticket.write_text(ticket.read_text() + "\nForged-After-Backfill: yes\n")
        self.commit("change protected source ticket")
        command("git", "-C", self.repo, "push", "-q", "--force", "origin", "main")
        command("git", "-C", self.repo, "fetch", "-q", "origin")
        with self.assertRaises(ValidationError):
            protected_terminal(self.repo, "T-001")

    def test_wrong_batch_repo_target_basis_cutoff_and_source_state_refuse(self):
        cases = (
            lambda value: value["tickets"].pop(),
            lambda value: value.update(repository="other/widget"),
            lambda value: value.update(target_kit_sha="bad"),
            lambda value: value["protected_main_basis"].update(tree="f" * 40),
            lambda value: value.update(cutoff="2020-01-01T00:00:00Z"),
        )
        original = self.request.read_text()
        for mutation in cases:
            self.request.write_text(original)
            self.mutate_request(mutation)
            self.assertNotEqual(self.generate().returncode, 0)
        self.request.write_text(original)
        ticket = self.repo / "factory/tickets/T-001.md"
        ticket.write_text(ticket.read_text().replace("State: Done", "State: Review"))
        self.commit("source is not done")
        command("git", "-C", self.repo, "push", "-q", "--force", "origin", "main")
        command("git", "-C", self.repo, "fetch", "-q", "origin")
        value = json.loads(self.request.read_text())
        value["protected_main_basis"] = {
            "commit": self.rev("HEAD"), "tree": self.rev("HEAD^{tree}"),
        }
        self.request.write_text(json.dumps(value))
        result = self.generate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source state", result.stderr)

    def test_wrong_app_duplicate_missing_failed_checks_and_bad_pr_ancestry_refuse(self):
        self.make_gh(wrong_app=True)
        self.assertIn("wrong-app", self.generate().stderr)
        self.make_gh(duplicate_check=True)
        self.assertIn("ambiguous", self.generate().stderr)
        self.make_gh(missing_check=True)
        self.assertIn("missing", self.generate().stderr)
        self.make_gh(failed_check=True)
        self.assertIn("failed", self.generate().stderr)
        self.make_gh(bad_closeout=True)
        self.assertNotEqual(self.generate().returncode, 0)

    def test_ticket_change_extra_file_and_normal_conflict_fail_closed(self):
        self.assertEqual(self.generate().returncode, 0)
        self.publish_generated()
        (self.repo / MIGRATION_DIR / "extra.json").write_text("{}\n")
        self.commit("add extra receipt")
        self.push()
        with self.assertRaises(ValidationError):
            protected_terminal(self.repo, "T-001")
        command("git", "-C", self.repo, "reset", "--hard", "-q", "HEAD^")
        normal = self.repo / "factory/attestations/T-001/done.json"
        normal.parent.mkdir(parents=True)
        normal.write_text('{"schema":"nysa.software-factory.ticket-done/v1"}\n')
        self.commit("add conflicting partial normal evidence")
        command("git", "-C", self.repo, "push", "-q", "--force", "origin", "main")
        command("git", "-C", self.repo, "fetch", "-q", "origin")
        with self.assertRaises(ValidationError):
            protected_terminal(self.repo, "T-001")


if __name__ == "__main__":
    unittest.main()
