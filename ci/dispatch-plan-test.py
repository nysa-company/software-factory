#!/usr/bin/env python3
"""Atomic deterministic dispatch selection, worktree, and claim tests."""

from __future__ import annotations

from contextlib import redirect_stdout
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "dispatch-plan.py"
SPEC = importlib.util.spec_from_file_location("dispatch_plan", HELPER)
assert SPEC and SPEC.loader
DISPATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DISPATCH)
import operator_receipt  # noqa: E402


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
            "factory/operator-map.json\nfactory/.dispatch-leases/\n"
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
        self.mapping = factory / "operator-map.json"
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
            "Product-Decisions: frozen\n"
            "Builder ownership: README.md only\n"
            "Fixture-Seams: none\n"
            "Authentication-Seams: none\n"
            "Protected-Test-Conflicts: none\n"
        )

    def write_mapping(self, states=None):
        observed = dt.datetime.now(dt.timezone.utc)
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

    def write_qualification_mapping(self, tickets):
        self.write_mapping(states={ticket: "Ready" for ticket in tickets})
        mapping = json.loads(self.mapping.read_text())
        for ticket in tickets:
            mapping["tickets"][ticket]["operator_fields_initialized"] = True
        self.mapping.write_text(json.dumps(mapping) + "\n")

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

    def write_contract_18_qualification(
        self, target=4, dependencies=None, successor=False,
        contract_version="1.8.0",
    ):
        tickets = [f"T-{number}" for number in range(110, 110 + target)]
        for ticket in tickets:
            self.ticket(ticket, "normal", "Ready")
        for ticket, required in (dependencies or {}).items():
            path = self.product / "factory/tickets" / f"{ticket}.md"
            path.write_text(
                path.read_text() + f"Depends-On: {','.join(required)}\n"
            )
        (self.product / "factory/PROJECT.env").write_text(
            f"TICKET_BRANCH_PREFIX=ticket/\nMAX_CONCURRENT_TICKETS={target}\n"
        )
        manifest = {
            "budget_usd": "300.000000" if successor else "100.000000",
            "capacity": target,
            "contract_version": contract_version,
            "factory_sha": "a" * 40,
            "generation": 1,
            "per_run_budget_usd": "10.000000" if successor else "2.000000",
            "per_ticket_budget_usd": "100.000000" if successor else "25.000000",
            "schema": "nysa.software-factory.qualification/v2",
            "target_done": target,
            "tickets": tickets,
        }
        if successor:
            manifest.update({
                "mode": "successor",
                "source_factory_sha": "b" * 40,
            })
        (self.product / "factory/QUALIFICATION.json").write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        )
        self.write_qualification_mapping(tickets)
        return tickets

    def stale_preprovider_branch(self, change_spec=False, materialize=False):
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
        if materialize:
            ticket_path.write_text(
                ticket_path.read_text().replace("State: Backlog", "State: Ready")
            )
            run("git", "add", str(ticket_path), cwd=self.product)
            run(
                "git", "-c", "user.name=Software Factory",
                "-c", "user.email=factory@local", "commit", "-qm",
                f"{ticket}: materialize ticket state", cwd=self.product,
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
        path.parent.mkdir(exist_ok=True)
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

    def command(
        self, action, expected=0, operator_map=None,
        contract=None, controller_state=None,
    ):
        environment = {
            **os.environ,
            "FACTORY_CERTIFIED_PRODUCT_ORIGIN": str(self.remote),
        }
        if operator_map is not None:
            environment["FACTORY_OPERATOR_MAP"] = str(operator_map)
        if contract is not None:
            environment["FACTORY_RELEASE_CONTRACT_VERSION"] = contract
        if controller_state is not None:
            environment["FACTORY_CONTROLLER_STATE_DIR"] = str(controller_state)
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
            env=environment,
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

    def test_shadow_uses_launcher_bound_operator_map(self):
        external = self.root / "operator-map.json"
        external.write_bytes(self.mapping.read_bytes())
        self.mapping.write_text("{}\n")
        value = self.command("shadow", operator_map=external)
        self.assertEqual(value["ticket"], "T-200")
        self.assertEqual(value["status"], "SHADOW")

    def test_contract_19_dispatch_requires_exact_open_operator_receipt(self):
        state = self.root / "controller"
        state.mkdir(mode=0o700)
        receipt = operator_receipt.issue(state, "T-300", "ready", {})
        operator = {
            "state": "Ready", "state_base": "backlog",
            "observed_at": receipt["issued_at"],
            "receipt_sha256": receipt["receipt_sha256"],
        }
        mapping = {"tickets": {"T-300": {"operator": operator}}}
        self.mapping.write_text(json.dumps(mapping) + "\n")
        environment = {
            "FACTORY_RELEASE_CONTRACT_VERSION": "1.9.0",
            "FACTORY_CONTROLLER_STATE_DIR": str(state),
        }
        with mock.patch.dict(os.environ, environment):
            selected, _ = DISPATCH.candidates(
                self.product / "factory", DISPATCH.load_mapping(self.mapping), set(),
            )
        self.assertIn("T-300", {item["ticket"] for item in selected})

        for mutation in ("forged", "consumed", "missing-state"):
            with self.subTest(mutation=mutation):
                operator["receipt_sha256"] = (
                    "f" * 64 if mutation == "forged"
                    else receipt["receipt_sha256"]
                )
                self.mapping.write_text(json.dumps(mapping) + "\n")
                if mutation == "consumed" and operator_receipt.peek_exact(
                    state, "T-300", "ready", receipt["receipt_sha256"],
                ):
                    operator_receipt.verify_consume_exact(
                        state, "T-300", "ready", receipt["receipt_sha256"],
                    )
                environment["FACTORY_CONTROLLER_STATE_DIR"] = (
                    str(self.root / "missing") if mutation == "missing-state"
                    else str(state)
                )
                with (
                    mock.patch.dict(os.environ, environment),
                    self.assertRaises((ValueError, OSError)),
                ):
                    DISPATCH.candidates(
                        self.product / "factory",
                        DISPATCH.load_mapping(self.mapping), set(),
                    )

    def test_durable_pre_dispatch_branch_is_truth_without_operator_map(self):
        self.write_mapping()
        self.ticket("T-500", "normal", "Backlog")
        run("git", "add", "factory/tickets/T-500.md", cwd=self.product)
        run("git", "commit", "-qm", "add cancellation fixture", cwd=self.product)
        run("git", "push", "-q", "origin", "main", cwd=self.product)
        for ticket, state in (("T-300", "Ready"), ("T-500", "Canceled")):
            branch = f"ticket/{ticket}"
            cell = self.root / f"durable-{ticket}"
            run(
                "git", "worktree", "add", "-q", "-b", branch, str(cell),
                cwd=self.product,
            )
            path = cell / f"factory/tickets/{ticket}.md"
            path.write_text(
                path.read_text().replace("State: Backlog", f"State: {state}")
            )
            run("git", "add", str(path), cwd=cell)
            run("git", "commit", "-qm", f"durable {state}", cwd=cell)
            run("git", "push", "-qu", "origin", branch, cwd=cell)
            run(
                "git", "worktree", "remove", "--force", str(cell),
                cwd=self.product,
            )

        selected, _ = DISPATCH.candidates(
            self.product / "factory", DISPATCH.load_mapping(self.mapping), set(),
        )
        selected_tickets = {item["ticket"] for item in selected}
        self.assertIn("T-300", selected_tickets)
        self.assertNotIn("T-500", selected_tickets)

    def test_readiness_refusal_is_ticket_local(self):
        ticket = self.product / "factory/tickets/T-200.md"
        ticket.write_text(ticket.read_text().replace(
            "Builder ownership: README.md only\n", ""
        ))
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "make one ticket unready", cwd=self.product)
        run("git", "push", "-q", "origin", "main", cwd=self.product)

        value = self.command("shadow")

        self.assertEqual(value["ticket"], "T-100")
        self.assertEqual(value["admission_refusal"], {
            "error": "provider-free ticket readiness contract is not executable",
            "reason_code": "invalid_ticket_contract",
            "ticket": "T-200",
        })

    def test_readiness_errors_fail_closed(self):
        failures = (
            (subprocess.CompletedProcess([], 1, "READINESS BLOCKED\n", ""), None),
            (subprocess.CompletedProcess([], 0, "not readiness evidence\n", ""), None),
            (None, subprocess.TimeoutExpired(["ticket-readiness"], 120)),
            (None, OSError("unavailable")),
        )
        for result, error in failures:
            with self.subTest(error=type(error).__name__ if error else "result"):
                with mock.patch.object(
                    DISPATCH.subprocess, "run", return_value=result, side_effect=error,
                ):
                    self.assertFalse(
                        DISPATCH.readiness_executable(self.product, "T-200")
                    )

    def test_readiness_refusal_precedes_claim_and_readmits_after_fix(self):
        for name in ("T-100", "T-300", "T-400"):
            path = self.product / f"factory/tickets/{name}.md"
            path.write_text(path.read_text().replace("State: Ready", "State: Backlog"))
        ticket = self.product / "factory/tickets/T-200.md"
        ticket.write_text(ticket.read_text().replace(
            "Builder ownership: README.md only\n", ""
        ))
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "leave one unready ticket", cwd=self.product)
        run("git", "push", "-q", "origin", "main", cwd=self.product)
        head = run("git", "rev-parse", "HEAD", cwd=self.product).strip()

        for action in ("shadow", "claim"):
            with self.subTest(action=action):
                value = self.command(action)
                self.assertEqual(value["action"], "WAIT")
                self.assertEqual(value["reason_code"], "no_candidate")
                self.assertEqual(value["admission_refusal"]["ticket"], "T-200")
                self.assertFalse((self.product / "factory/.dispatch-leases").exists())
                self.assertEqual(list(self.worktrees.glob("cell-*")), [])
                self.assertEqual(
                    run("git", "rev-parse", "HEAD", cwd=self.product).strip(), head
                )
                self.assertEqual(
                    run("git", "branch", "--format=%(refname:short)", cwd=self.product),
                    "main\n",
                )

        ticket.write_text(ticket.read_text().replace(
            "Product-Decisions: frozen\n",
            "Product-Decisions: frozen\nBuilder ownership: README.md only\n",
        ))
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "repair ticket readiness", cwd=self.product)
        run("git", "push", "-q", "origin", "main", cwd=self.product)

        value = self.command("claim")

        self.assertEqual(value["ticket"], "T-200")
        self.assertEqual(Path(value["worktree"]).name, "cell-1")
        self.assertTrue(
            (self.product / "factory/.dispatch-leases/T-200.json").is_file()
        )

    def test_protected_canceled_ticket_cannot_be_resurrected_by_resume_overlay(self):
        parked = self.worktrees / "parked/T-200"
        parked.parent.mkdir()
        run(
            "git", "worktree", "add", "-q", "-b", "ticket/T-200",
            str(parked), cwd=self.product,
        )
        branch_ticket = parked / "factory/tickets/T-200.md"
        branch_ticket.write_text(
            branch_ticket.read_text().replace("State: Ready", "State: Planning")
        )
        run("git", "add", str(branch_ticket), cwd=parked)
        run("git", "commit", "-qm", "retain stale planning ticket", cwd=parked)
        run("git", "push", "-qu", "origin", "ticket/T-200", cwd=parked)
        branch_head = run("git", "rev-parse", "HEAD", cwd=parked).strip()
        ticket = self.product / "factory/tickets/T-200.md"
        ticket.write_text(ticket.read_text().replace("State: Ready", "State: Canceled"))
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "cancel ticket on protected main", cwd=self.product)
        run("git", "push", "-q", "origin", "main", cwd=self.product)
        self.write_mapping(states={"T-200": "Planning"})

        value = self.command("claim")

        self.assertEqual(value["ticket"], "T-100")
        self.assertFalse(
            (self.product / "factory/.dispatch-leases/T-200.json").exists()
        )
        self.assertEqual(run("git", "rev-parse", "HEAD", cwd=parked).strip(), branch_head)

    def test_protected_done_ticket_cannot_be_resurrected_by_resume_overlay(self):
        ticket = self.product / "factory/tickets/T-200.md"
        ticket.write_text(ticket.read_text().replace("State: Ready", "State: Done"))
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "finish ticket on protected main", cwd=self.product)
        run("git", "push", "-q", "origin", "main", cwd=self.product)
        self.write_mapping(states={"T-200": "Planning"})

        value = self.command("shadow")

        self.assertEqual(value["ticket"], "T-100")

    def test_nonqualification_dispatch_waits_for_protected_dependency(self):
        ticket = self.product / "factory/tickets/T-200.md"
        ticket.write_text(ticket.read_text() + "Depends-On: T-300\n")
        with mock.patch.object(
            DISPATCH,
            "protected_dependency",
            side_effect=DISPATCH.ValidationError("not terminal"),
        ):
            selected, refusals = DISPATCH.candidates(
                self.product / "factory",
                DISPATCH.load_mapping(self.mapping),
                set(),
            )
        self.assertNotIn("T-200", {item["ticket"] for item in selected})
        self.assertIn("T-100", {item["ticket"] for item in selected})
        self.assertEqual(refusals, [])

    def test_ineligible_cohort_skips_protected_dependency_validation(self):
        for number in range(500, 564):
            self.ticket(f"T-{number}", "normal", "Backlog")
            path = self.product / f"factory/tickets/T-{number}.md"
            path.write_text(path.read_text() + "Depends-On: T-999\n")
        self.write_mapping(states={"T-300": "Ready"})
        effective = self.product / "factory/tickets/T-300.md"
        effective.write_text(effective.read_text() + "Depends-On: T-997\n")
        stale = self.product / "factory/tickets/T-400.md"
        stale.write_text(
            stale.read_text() + f"Kit-SHA: {'b' * 40}\nDepends-On: T-996\n"
        )
        eligible = self.product / "factory/tickets/T-200.md"
        eligible.write_text(eligible.read_text() + "Depends-On: T-998\n")

        def protected_dependency(_product, dependency):
            if dependency == "T-998":
                raise DISPATCH.ValidationError("not terminal")

        with mock.patch.object(
            DISPATCH,
            "protected_dependency",
            side_effect=protected_dependency,
        ) as protected:
            selected, refusals = DISPATCH.candidates(
                self.product / "factory",
                DISPATCH.load_mapping(self.mapping),
                set(),
            )

        self.assertEqual(protected.call_args_list, [
            mock.call(self.product, "T-998"),
            mock.call(self.product, "T-997"),
        ])
        selected_tickets = {item["ticket"] for item in selected}
        self.assertNotIn("T-200", selected_tickets)
        self.assertIn("T-300", selected_tickets)
        self.assertEqual(refusals, [])

    def test_malformed_non_goal_dependency_is_named_without_blocking_siblings(self):
        ticket = self.product / "factory/tickets/T-300.md"
        ticket.write_text(
            ticket.read_text()
            + "Depends-On: none — prose is not a dependency literal\n"
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "add malformed non-goal ticket", cwd=self.product)
        run("git", "push", "-q", "origin", "main", cwd=self.product)

        value = self.command("shadow")

        self.assertEqual(value["ticket"], "T-200")
        self.assertEqual(value["admission_refusal"], {
            "error": "ticket dependencies are invalid",
            "reason_code": "invalid_ticket_contract",
            "ticket": "T-300",
        })

    def test_malformed_only_ticket_returns_named_wait(self):
        for ticket in ("T-100", "T-200", "T-400"):
            path = self.product / f"factory/tickets/{ticket}.md"
            path.write_text(path.read_text().replace("State: Ready", "State: Backlog"))
        malformed = self.product / "factory/tickets/T-300.md"
        malformed.write_text(
            malformed.read_text().replace("State: Backlog", "State: Ready")
            + "Depends-On: not-a-ticket\n"
        )
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "leave one malformed eligible ticket", cwd=self.product)
        run("git", "push", "-q", "origin", "main", cwd=self.product)

        value = self.command("shadow")

        self.assertEqual(value["action"], "WAIT")
        self.assertEqual(value["reason_code"], "no_candidate")
        self.assertEqual(value["admission_refusal"]["ticket"], "T-300")

    def test_null_operator_initiative_names_ticket_and_admits_sibling(self):
        self.write_mapping(states={"T-200": "Ready"})
        mapping = json.loads(self.mapping.read_text())
        mapping["tickets"]["T-200"]["operator"]["initiative"] = None
        self.mapping.write_text(json.dumps(mapping) + "\n")

        value = self.command("shadow")

        self.assertEqual(value["ticket"], "T-100")
        self.assertEqual(value["admission_refusal"], {
            "error": "ticket initiative is missing",
            "reason_code": "initiative_missing",
            "ticket": "T-200",
        })

    def test_qualification_null_operator_initiative_is_named_not_silent(self):
        self.write_contract_18_qualification()
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "add qualification", cwd=self.product)
        run("git", "push", "-q", "origin", "main", cwd=self.product)
        mapping = json.loads(self.mapping.read_text())
        mapping["tickets"]["T-110"]["operator"]["initiative"] = None
        self.mapping.write_text(json.dumps(mapping) + "\n")

        value = self.command("shadow")

        self.assertEqual(value["ticket"], "T-111")
        self.assertEqual(value["admission_refusal"]["reason_code"], "initiative_missing")
        self.assertEqual(value["admission_refusal"]["ticket"], "T-110")

    def test_malformed_dependency_shapes_share_one_ticket_refusal(self):
        path = self.product / "factory/tickets/T-300.md"
        original = path.read_text()
        for suffix in (
            "Depends-On: T-100,T-100\n",
            "Depends-On: invalid\n",
            "Depends-On: T-100\nDepends-On: T-200\n",
        ):
            with self.subTest(suffix=suffix):
                path.write_text(original + suffix)
                selected, refusals = DISPATCH.candidates(
                    self.product / "factory",
                    DISPATCH.load_mapping(self.mapping),
                    set(),
                )
                self.assertIn("T-200", {item["ticket"] for item in selected})
                self.assertEqual(refusals[0]["ticket"], "T-300")
                self.assertEqual(
                    refusals[0]["reason_code"], "invalid_ticket_contract"
                )

    def test_selected_qualification_dependency_remains_fail_closed(self):
        self.write_qualification()
        path = self.product / "factory/tickets/T-100.md"
        path.write_text(path.read_text() + "Depends-On: invalid\n")

        with self.assertRaisesRegex(
            DISPATCH.DispatchError, "ticket dependencies are invalid"
        ):
            DISPATCH.qualification(self.product, self.product / "factory", 4)

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

    def test_slow_candidate_resolution_does_not_hold_launch_lock(self):
        entered = threading.Event()
        proceed = threading.Event()
        output = io.StringIO()
        errors = []
        original = DISPATCH.candidates

        def delayed(*args, **kwargs):
            entered.set()
            if not proceed.wait(5):
                raise AssertionError("candidate resolution was not released")
            return original(*args, **kwargs)

        def invoke():
            try:
                with (
                    mock.patch.object(
                        sys,
                        "argv",
                        [
                            str(HELPER),
                            "--factory-root",
                            str(self.product),
                            "--worktree-root",
                            str(self.worktrees),
                            "claim",
                        ],
                    ),
                    mock.patch.dict(
                        os.environ,
                        {"FACTORY_CERTIFIED_PRODUCT_ORIGIN": str(self.remote)},
                    ),
                    redirect_stdout(output),
                ):
                    DISPATCH.main()
            except BaseException as error:
                errors.append(error)

        with mock.patch.object(DISPATCH, "candidates", side_effect=delayed):
            thread = threading.Thread(target=invoke)
            thread.start()
            self.assertTrue(entered.wait(5))
            launch_lock = self.product / "factory/.launch.lock"
            launch_lock.mkdir(mode=0o700)
            launch_lock.rmdir()
            proceed.set()
            thread.join(10)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(json.loads(output.getvalue())["status"], "CLAIMED")

    def test_ticket_identity_survives_cell_relocation(self):
        first = self.command("claim")
        old_cell = Path(first["worktree"])
        new_cell = self.worktrees / "cell-4"
        (self.product / "factory/.dispatch-leases/T-200.json").unlink()
        run("git", "worktree", "move", str(old_cell), str(new_cell), cwd=self.product)

        resumed = self.command("claim")
        self.assertEqual(resumed["ticket"], "T-200")
        self.assertEqual(Path(resumed["worktree"]), new_cell)

    def test_maintenance_and_dirty_root_refuse(self):
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

    def test_post_selection_refusal_names_the_selected_ticket(self):
        outside = self.root / "parked/T-200"
        outside.parent.mkdir()
        run(
            "git", "worktree", "add", "-q", "-b", "ticket/T-200",
            str(outside), cwd=self.product,
        )

        value = self.command("claim", expected=2)

        self.assertIn("outside a trusted cell", value["error"])
        self.assertEqual(value["ticket"], "T-200")

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

    def test_readiness_refusal_precedes_authorized_branch_reset(self):
        self.write_contract_18_qualification()
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(ticket.read_text().replace(
            "Builder ownership: README.md only\n", ""
        ))
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "leave reset ticket unready", cwd=self.product)
        run("git", "push", "-q", "origin", "main", cwd=self.product)
        old_head = self.stale_preprovider_branch()
        self.authorize_preprovider_reset(old_head)

        value = self.command("claim")

        self.assertEqual(value["ticket"], "T-111")
        self.assertEqual(value["admission_refusal"]["ticket"], "T-110")
        remote_head = run(
            "git", "ls-remote", "--heads", str(self.remote), "ticket/T-110"
        ).split()[0]
        self.assertEqual(remote_head, old_head)

    def test_authorized_reset_does_not_adopt_an_outside_worktree(self):
        self.write_contract_18_qualification()
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "prepare qualification", cwd=self.product)
        run("git", "push", "-q", "origin", "main", cwd=self.product)
        old_head = self.stale_preprovider_branch()
        outside = self.root / "predecessor-cell"
        run(
            "git", "worktree", "add", "-q", str(outside), "ticket/T-110",
            cwd=self.product,
        )
        self.authorize_preprovider_reset(old_head)

        value = self.command("claim", expected=2)

        self.assertIn("outside a trusted cell", value["error"])
        self.assertFalse(any(
            path.name.startswith("cell-") for path in self.worktrees.iterdir()
        ))

    def test_authorized_materialize_lineage_rejoins_current_main(self):
        self.write_contract_18_qualification()
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(ticket.read_text().replace("State: Ready", "State: Backlog"))
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "prepare backlog qualification", cwd=self.product)
        run("git", "push", "-q", "origin", "main", cwd=self.product)
        old_head = self.stale_preprovider_branch(materialize=True)
        ticket.write_text(ticket.read_text().replace("State: Backlog", "State: Ready"))
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "materialize protected ticket", cwd=self.product)
        run("git", "push", "-q", "origin", "main", cwd=self.product)
        self.authorize_preprovider_reset(old_head)

        value = self.command("claim")

        worktree = Path(value["worktree"])
        self.assertEqual(value["preprovider_reset_head"], old_head)
        self.assertIn(
            "State: Ready",
            (worktree / "factory/tickets/T-110.md").read_text(),
        )
        subjects = run(
            "git", "log", "--format=%s", f"{old_head}~3..{old_head}",
            cwd=self.product,
        ).splitlines()
        self.assertIn("T-110: materialize ticket state", subjects)

    def test_invalid_operator_map_cannot_deadlock_authorized_control_reset(self):
        tickets = self.write_contract_18_qualification()
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(ticket.read_text() + "Merge-Policy: auto\n")
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "prepare qualification", cwd=self.product)
        run("git", "push", "-q", "origin", "main", cwd=self.product)
        old_head = self.stale_preprovider_branch()
        ticket.write_text(ticket.read_text().replace(
            "Merge-Policy: auto", "Merge-Policy: manual"
        ))
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "correct protected merge policy", cwd=self.product)
        run("git", "push", "-q", "origin", "main", cwd=self.product)
        self.authorize_preprovider_reset(old_head)
        self.write_qualification_mapping(tickets)
        mapping = json.loads(self.mapping.read_text())
        mapping["tickets"]["T-110"]["operator_fields_initialized"] = False
        self.mapping.write_text(json.dumps(mapping) + "\n")

        invalid = self.command("claim", expected=2)

        self.assertEqual(
            invalid["error"],
            "selected-ticket operator projection is invalid: T-110",
        )
        remote_head = run(
            "git", "ls-remote", "--heads", str(self.remote), "ticket/T-110"
        ).split()[0]
        self.assertNotEqual(remote_head, old_head)
        self.assertFalse((self.product / "factory/.dispatch-leases/T-110.json").exists())
        self.write_qualification_mapping(tickets)
        value = self.command("claim")
        self.assertEqual(value["ticket"], "T-110")

    def test_qualification_operator_map_is_local_projection_without_staleness(self):
        tickets = self.write_contract_18_qualification()
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "prepare projection qualification", cwd=self.product)
        run("git", "push", "-q", "origin", "main", cwd=self.product)
        self.write_qualification_mapping(tickets)
        mapping = json.loads(self.mapping.read_text())
        stale = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=100_000)
        ).isoformat()
        mapping["_sync"] = {
            "last_error": "operator projection noise",
            "last_success_at": stale,
        }
        for ticket in tickets:
            mapping["tickets"][ticket]["operator"]["observed_at"] = stale
        # A ticket without a projection entry defaults benignly: no overrides.
        mapping["tickets"].pop(tickets[-1])
        self.mapping.write_text(json.dumps(mapping) + "\n")

        value = self.command("shadow")

        self.assertIn(value["ticket"], tickets)
        self.assertEqual(value["status"], "SHADOW")

        cases = {
            "uninitialized": lambda item: item["tickets"][tickets[0]].update(
                operator_fields_initialized=False
            ),
            "operator-missing": lambda item: item["tickets"][tickets[0]].pop(
                "operator"
            ),
            "entry-not-object": lambda item: item["tickets"].update(
                {tickets[0]: "not-an-object"}
            ),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                unhealthy = json.loads(json.dumps(mapping))
                mutate(unhealthy)
                self.mapping.write_text(json.dumps(unhealthy) + "\n")
                refused = self.command("shadow", expected=2)
                self.assertIn(
                    "selected-ticket operator projection is invalid",
                    refused["error"],
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
        self.assertFalse(
            any(path.name.startswith("cell-") for path in self.worktrees.iterdir())
        )

    def test_repeated_authorized_control_recovery_preserves_lineage(self):
        self.write_contract_18_qualification()
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "prepare qualification", cwd=self.product)
        run("git", "push", "-q", "origin", "main", cwd=self.product)
        self.authorize_preprovider_reset(self.stale_preprovider_branch())
        first = self.command("claim")
        worktree = Path(first["worktree"])
        ticket = worktree / "factory/tickets/T-110.md"
        ticket.write_text(ticket.read_text() + f"\nKit-SHA: {'c' * 40}\n")
        plan = worktree / "factory/route-plans/T-110.json"
        plan.parent.mkdir(exist_ok=True)
        plan.write_text(json.dumps({
            "kit_sha": "c" * 40,
            "schema": "ticket-model-route-plan/v1",
            "ticket": "T-110",
        }) + "\n")
        run("git", "add", str(ticket), str(plan), cwd=worktree)
        run(
            "git", "-c", "user.name=Software Factory",
            "-c", "user.email=factory@local", "commit", "-qm",
            "T-110: pin kit and model route plan", cwd=worktree,
        )
        ticket.write_text(ticket.read_text().replace("State: Ready", "State: Planning"))
        run("git", "add", str(ticket), cwd=worktree)
        run(
            "git", "-c", "user.name=Software Factory",
            "-c", "user.email=factory@local", "commit", "-qm",
            "T-110: transition ticket state", cwd=worktree,
        )
        repeated_head = run("git", "rev-parse", "HEAD", cwd=worktree).strip()
        run("git", "push", "-q", "origin", "ticket/T-110", cwd=worktree)
        (self.product / "factory/.dispatch-leases/T-110.json").unlink()
        run("git", "worktree", "remove", str(worktree), cwd=self.product)
        run("git", "branch", "-D", "ticket/T-110", cwd=self.product)
        self.authorize_preprovider_reset(repeated_head)

        value = self.command("claim")

        recovered = Path(value["worktree"])
        self.assertEqual(value["preprovider_reset_head"], repeated_head)
        self.assertEqual(
            run("git", "rev-parse", "HEAD^{tree}", cwd=recovered),
            run("git", "rev-parse", "origin/main^{tree}", cwd=recovered),
        )

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

    def test_contract_18_qualification_accepts_four_independent_canaries(self):
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

    def test_current_contract_qualifications_accept_only_supported_versions(self):
        for contract in ("1.8.0", "1.9.0"):
            with self.subTest(contract=contract):
                self.write_contract_18_qualification(
                    contract_version=contract,
                )
                with mock.patch.object(
                    DISPATCH, "protected_terminal",
                    side_effect=DISPATCH.ValidationError("not done"),
                ):
                    value = DISPATCH.qualification(
                        self.product, self.product / "factory", 4
                    )
                self.assertEqual(value["contract_version"], contract)

        for contract in ("1.7.0", "2.0.0"):
            with self.subTest(contract=contract):
                self.write_contract_18_qualification(
                    contract_version=contract,
                )
                with self.assertRaisesRegex(
                    DISPATCH.DispatchError, "qualification manifest is invalid",
                ):
                    DISPATCH.qualification(
                        self.product, self.product / "factory", 4
                    )

    def test_claim_rechecks_presealed_ticket_blob_before_worktree(self):
        self.write_contract_18_qualification()
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "local qualification control", cwd=self.product)

        value = self.command("claim", expected=2)

        self.assertIn(
            "qualification ticket source differs from protected dispatch",
            value["error"],
        )
        self.assertFalse((self.product / "factory/.dispatch-leases").exists())
        self.assertEqual(list(self.worktrees.iterdir()), [])

    def test_contract_18_qualification_accepts_ordered_three_ticket_cohort(self):
        tickets = self.write_contract_18_qualification(3, {
            "T-111": ["T-110"],
            "T-112": ["T-111", "T-099"],
        })

        def terminal(_product, ticket):
            if ticket != "T-099":
                raise DISPATCH.ValidationError("not done")
            return {"ticket": ticket}

        with mock.patch.object(DISPATCH, "protected_terminal", side_effect=terminal):
            value = DISPATCH.qualification(
                self.product, self.product / "factory", 3
            )
        self.assertEqual(value["tickets"], tickets)
        self.assertEqual(value["capacity"], 3)
        self.assertEqual(value["dependencies"]["T-112"], ("T-111", "T-099"))
        self.assertEqual(value["terminal"], {"T-099"})

        path = self.product / "factory/tickets/T-110.md"
        path.write_text(path.read_text() + "Depends-On: T-112\n")
        with self.assertRaisesRegex(DISPATCH.DispatchError, "cycle"):
            DISPATCH.qualification(self.product, self.product / "factory", 3)

    def test_contract_18_successor_qualification_uses_production_envelope(self):
        tickets = self.write_contract_18_qualification(3, {
            "T-111": ["T-110"], "T-112": ["T-111"],
        }, successor=True)
        with mock.patch.object(
            DISPATCH, "protected_terminal", side_effect=DISPATCH.ValidationError("not done")
        ):
            value = DISPATCH.qualification(
                self.product, self.product / "factory", 3
            )
        self.assertEqual(value["tickets"], tickets)
        self.assertEqual(value["mode"], "successor")
        self.assertEqual(value["source_factory_sha"], "b" * 40)

if __name__ == "__main__":
    unittest.main()
