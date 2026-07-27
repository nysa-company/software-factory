#!/usr/bin/env python3
"""Atomic deterministic dispatch selection, worktree, and claim tests."""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "dispatch-plan.py"
SPEC = importlib.util.spec_from_file_location("dispatch_plan", HELPER)
assert SPEC and SPEC.loader
DISPATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DISPATCH)


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

    def write_qualification(self, dependencies=None):
        tickets = [f"T-{number}" for number in range(100, 110)]
        for ticket in tickets:
            self.ticket(ticket, "normal", "Ready")
        for ticket, required in (dependencies or {}).items():
            path = self.product / "factory/tickets" / f"{ticket}.md"
            path.write_text(
                path.read_text() + f"Depends-On: {','.join(required)}\n"
            )
        manifest = {
            "factory_sha": "a" * 40,
            "final_capacity": 4,
            "generation": 1,
            "initial_capacity": 3,
            "ramp_after_done": 3,
            "schema": "nysa.software-factory.qualification/v1",
            "target_done": 10,
            "tickets": tickets,
        }
        (self.product / "factory/QUALIFICATION.json").write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        )
        return tickets

    def write_contract_18_qualification(self):
        tickets = [f"T-{number}" for number in range(110, 114)]
        for ticket in tickets:
            self.ticket(ticket, "normal", "Ready")
        (self.product / "factory/PROJECT.env").write_text(
            "TICKET_BRANCH_PREFIX=ticket/\nMAX_CONCURRENT_TICKETS=4\n"
        )
        manifest = {
            "budget_usd": "100.000000",
            "capacity": 4,
            "contract_version": "1.8.0",
            "factory_sha": "a" * 40,
            "generation": 1,
            "per_run_budget_usd": "2.000000",
            "per_ticket_budget_usd": "25.000000",
            "schema": "nysa.software-factory.qualification/v2",
            "target_done": 4,
            "tickets": tickets,
        }
        (self.product / "factory/QUALIFICATION.json").write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        )
        return tickets

    def stale_preprovider_branch(self, change_spec=False):
        ticket = "T-110"
        branch = f"ticket/{ticket}"
        run("git", "switch", "-qc", branch, cwd=self.product)
        ticket_path = self.product / f"factory/tickets/{ticket}.md"
        ticket_path.write_text(ticket_path.read_text() + f"\nKit-SHA: {'b' * 40}\n")
        plan = self.product / f"factory/route-plans/{ticket}.json"
        plan.parent.mkdir()
        plan.write_text(json.dumps({
            "kit_sha": "b" * 40,
            "schema": "ticket-model-route-plan/v1",
            "ticket": ticket,
        }) + "\n")
        run("git", "add", str(ticket_path), str(plan), cwd=self.product)
        run(
            "git", "-c", "user.name=Software Factory",
            "-c", "user.email=factory@local", "commit", "-qm",
            f"{ticket}: pin kit and model route plan", cwd=self.product,
        )
        text = ticket_path.read_text().replace("State: Ready", "State: Planning")
        if change_spec:
            text += "\nProvider-authored specification drift.\n"
        ticket_path.write_text(text)
        run("git", "add", str(ticket_path), cwd=self.product)
        run(
            "git", "-c", "user.name=Software Factory",
            "-c", "user.email=factory@local", "commit", "-qm",
            f"{ticket}: transition ticket state", cwd=self.product,
        )
        head = run("git", "rev-parse", "HEAD", cwd=self.product).strip()
        run("git", "push", "-qu", "origin", branch, cwd=self.product)
        run("git", "switch", "-q", "main", cwd=self.product)
        return head

    def authorize_preprovider_reset(self, head):
        path = self.product / "factory/qualification/preprovider-branch-resets.json"
        path.parent.mkdir()
        path.write_text(json.dumps({
            "factory_sha": "a" * 40,
            "resets": [{
                "branch": "ticket/T-110",
                "head": head,
                "ticket": "T-110",
            }],
            "schema": "nysa.software-factory.preprovider-branch-resets/v1",
        }, sort_keys=True, separators=(",", ":")) + "\n")
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "authorize pre-provider reset", cwd=self.product)
        run("git", "push", "-q", "origin", "main", cwd=self.product)

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
        self.assertEqual(worktree.name, "cell-1")
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

    def test_ticket_identity_survives_cell_relocation(self):
        first = self.command("claim")
        old_cell = Path(first["worktree"])
        new_cell = self.worktrees / "cell-4"
        (self.product / "factory/.dispatch-leases/T-200.json").unlink()
        run("git", "worktree", "move", str(old_cell), str(new_cell), cwd=self.product)

        resumed = self.command("claim")
        self.assertEqual(resumed["ticket"], "T-200")
        self.assertEqual(Path(resumed["worktree"]), new_cell)

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
        self.assertFalse((self.worktrees / "cell-3").exists())

    def test_failed_lease_write_removes_new_worktree_and_branch(self):
        lease_dir = self.product / "factory/.dispatch-leases"
        lease_dir.mkdir(mode=0o500)
        try:
            self.command("claim", expected=2)
        finally:
            lease_dir.chmod(0o700)
        self.assertFalse((self.worktrees / "cell-1").exists())
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

    def test_authorized_control_only_remote_branch_rejoins_current_main(self):
        self.write_contract_18_qualification()
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "prepare qualification", cwd=self.product)
        run("git", "push", "-q", "origin", "main", cwd=self.product)
        old_head = self.stale_preprovider_branch()
        self.authorize_preprovider_reset(old_head)

        value = self.command("claim")

        worktree = Path(value["worktree"])
        self.assertEqual(value["preprovider_reset_head"], old_head)
        self.assertEqual(
            run("git", "rev-parse", "HEAD^{tree}", cwd=worktree),
            run("git", "rev-parse", "origin/main^{tree}", cwd=worktree),
        )
        ticket = (worktree / "factory/tickets/T-110.md").read_text()
        self.assertIn("State: Ready", ticket)
        self.assertNotIn("Kit-SHA:", ticket)
        self.assertFalse((worktree / "factory/route-plans/T-110.json").exists())
        self.assertIn(
            "supersede pre-provider control state",
            run("git", "log", "-1", "--format=%s", cwd=worktree),
        )

    def test_authorized_reset_rejects_non_control_ticket_drift(self):
        self.write_contract_18_qualification()
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "prepare qualification", cwd=self.product)
        run("git", "push", "-q", "origin", "main", cwd=self.product)
        old_head = self.stale_preprovider_branch(change_spec=True)
        self.authorize_preprovider_reset(old_head)

        value = self.command("claim", expected=2)

        self.assertIn("control state is invalid", value["error"])
        self.assertEqual(list(self.worktrees.iterdir()), [])

    def test_qualification_ramps_filters_dependencies_and_completes(self):
        tickets = self.write_qualification({"T-109": ["T-100"]})

        def state(done):
            def terminal(_product, ticket):
                if ticket not in done:
                    raise DISPATCH.ValidationError("not done")
                return {"ticket": ticket}

            with mock.patch.object(DISPATCH, "protected_terminal", side_effect=terminal):
                return DISPATCH.qualification(
                    self.product, self.product / "factory", 4
                )

        initial = state(set())
        self.assertEqual(initial["capacity"], 3)
        self.assertNotIn("T-100", initial["terminal"])
        self.assertEqual(initial["dependencies"]["T-109"], ("T-100",))

        ramped = state(set(tickets[:3]))
        self.assertEqual(ramped["capacity"], 4)
        self.assertEqual(ramped["done"], 3)
        self.assertIn("T-100", ramped["terminal"])

        complete = state(set(tickets))
        self.assertEqual(complete["done"], complete["target_done"])

    def test_qualification_rejects_dependency_cycle(self):
        self.write_qualification({"T-100": ["T-101"], "T-101": ["T-100"]})
        with self.assertRaisesRegex(DISPATCH.DispatchError, "cycle"):
            DISPATCH.qualification(self.product, self.product / "factory", 4)

    def test_contract_18_qualification_requires_four_independent_canaries(self):
        tickets = self.write_contract_18_qualification()
        with mock.patch.object(
            DISPATCH, "protected_terminal", side_effect=DISPATCH.ValidationError("not done")
        ):
            value = DISPATCH.qualification(
                self.product, self.product / "factory", 4
            )
        self.assertEqual(value["tickets"], tickets)
        self.assertEqual(value["capacity"], 4)
        self.assertEqual(value["dependencies"], {ticket: () for ticket in tickets})
        self.assertEqual(value["done"], 0)

if __name__ == "__main__":
    unittest.main()
