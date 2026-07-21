#!/usr/bin/env python3
"""Atomic deterministic dispatch selection, worktree, and claim tests."""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "dispatch-plan.py"


def run(*command, cwd=None):
    return subprocess.run(
        command, cwd=cwd, text=True, capture_output=True, check=True
    ).stdout


class DispatchPlanTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        os.chmod(self.root, 0o700)
        self.remote = self.root / "remote.git"
        run("git", "init", "--bare", "-q", str(self.remote))
        self.product = self.root / "product"
        run("git", "init", "-q", "-b", "main", str(self.product))
        run("git", "config", "user.name", "Test", cwd=self.product)
        run("git", "config", "user.email", "test@example.invalid", cwd=self.product)
        factory = self.product / "factory"
        (factory / "tickets").mkdir(parents=True)
        (factory / "PROJECT.env").write_text(
            "TICKET_BRANCH_PREFIX=ticket/\nMAX_CONCURRENT_TICKETS=2\n"
        )
        (factory / "KIT_PIN").write_text("a" * 40 + "\n")
        (self.product / ".gitignore").write_text(
            "factory/linear-map.json\nfactory/.dispatch-leases/\n"
            "factory/.dispatch-leases.lock/\nfactory/.launch.lock/\n"
        )
        self.ticket("T-100", "normal", "Ready")
        self.ticket("T-200", "urgent", "Ready")
        self.ticket("T-300", "urgent", "Backlog")
        self.ticket("T-400", "low", "Ready")
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "seed", cwd=self.product)
        run("git", "remote", "add", "origin", str(self.remote), cwd=self.product)
        run("git", "push", "-qu", "origin", "main", cwd=self.product)
        self.mapping = factory / "linear-map.json"
        self.write_mapping()
        self.worktrees = self.root / "worktrees"
        self.worktrees.mkdir(mode=0o700)

    def tearDown(self):
        self.temp.cleanup()

    def ticket(self, ticket, priority, state):
        (self.product / "factory/tickets" / f"{ticket}.md").write_text(
            f"# {ticket}: test\n\nPriority: {priority}\n"
            "Initiative: I-1\n"
            f"State: {state}\nBranch: ticket/{ticket}\n"
        )

    def write_mapping(self, age=0, states=None):
        observed = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age)
        tickets = {}
        for ticket, state in (states or {}).items():
            tickets[ticket] = {
                "operator": {
                    "observed_at": observed.isoformat(),
                    "priority": (
                        "urgent" if ticket in ("T-200", "T-300") else "normal"
                    ),
                    "state": state,
                    "state_base": (
                        "backlog" if state == "Ready" else "blocked-escalated"
                    ),
                }
            }
        self.mapping.write_text(
            json.dumps(
                {
                    "_sync": {"last_success_at": observed.isoformat()},
                    "tickets": tickets,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )

    def command(self, action, expected=0):
        result = subprocess.run(
            [
                sys.executable, str(HELPER),
                "--factory-root", str(self.product),
                "--worktree-root", str(self.worktrees),
                action,
            ],
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, "FACTORY_CERTIFIED_PRODUCT_ORIGIN": str(self.remote)},
            timeout=30,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_shadow_is_deterministic_and_does_not_claim_or_prepare(self):
        value = self.command("shadow")
        self.assertEqual(value["ticket"], "T-200")
        self.assertEqual(value["status"], "SHADOW")
        self.assertFalse((self.product / "factory/.dispatch-leases").exists())
        self.assertEqual(list(self.worktrees.iterdir()), [])

    def test_claim_prepares_exact_worktree_then_next_claim_is_distinct(self):
        first = self.command("claim")
        self.assertEqual(first["ticket"], "T-200")
        self.assertRegex(first["lease_id"], r"^[0-9a-f]{64}$")
        worktree = Path(first["worktree"])
        self.assertEqual(
            run("git", "symbolic-ref", "--short", "HEAD", cwd=worktree).strip(),
            "ticket/T-200",
        )
        self.assertEqual(run("git", "status", "--porcelain", cwd=worktree), "")
        second = self.command("claim")
        self.assertEqual(second["ticket"], "T-100")
        self.assertNotEqual(first["lease_id"], second["lease_id"])

    def test_duplicate_wakeups_atomically_claim_distinct_tickets(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.command("claim"), range(2)))
        self.assertEqual({item["ticket"] for item in results}, {"T-100", "T-200"})
        self.assertEqual(len({item["lease_id"] for item in results}), 2)

    def test_stale_reconciliation_maintenance_and_dirty_root_refuse(self):
        self.write_mapping(age=601)
        self.assertIn("stale", self.command("shadow", expected=2)["error"])
        self.write_mapping()
        (self.product / "factory/MAINTENANCE").touch()
        self.assertIn("blocks dispatch", self.command("claim", expected=2)["error"])
        (self.product / "factory/MAINTENANCE").unlink()
        (self.product / "dirty.txt").write_text("dirty\n")
        self.assertIn("dirty", self.command("claim", expected=2)["error"])

    def test_full_capacity_waits_without_preparing_another_worktree(self):
        self.command("claim")
        self.command("claim")
        value = self.command("claim")
        self.assertEqual(value["action"], "WAIT")
        self.assertEqual(value["reason_code"], "capacity_full")
        self.assertFalse((self.worktrees / "T-400").exists())

    def test_failed_lease_write_removes_new_worktree_and_branch(self):
        lease_dir = self.product / "factory/.dispatch-leases"
        lease_dir.mkdir(mode=0o500)
        try:
            self.command("claim", expected=2)
        finally:
            lease_dir.chmod(0o700)
        self.assertFalse((self.worktrees / "T-200").exists())
        branches = run("git", "branch", "--format=%(refname:short)", cwd=self.product)
        self.assertNotIn("ticket/T-200", branches.splitlines())

    def test_reused_worktree_must_match_its_remote_or_fresh_main(self):
        claim = self.command("claim")
        worktree = Path(claim["worktree"])
        (worktree / "local-only.txt").write_text("divergent\n")
        run("git", "add", "local-only.txt", cwd=worktree)
        run("git", "commit", "-qm", "local divergence", cwd=worktree)
        (self.product / "factory/.dispatch-leases/T-200.json").unlink()
        value = self.command("claim", expected=2)
        self.assertIn("divergent or unpushed", value["error"])


if __name__ == "__main__":
    unittest.main()
