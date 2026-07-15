#!/usr/bin/env python3
import csv
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "ledger-view.py"
SPEC = importlib.util.spec_from_file_location("ledger_view", HELPER)
LEDGER_VIEW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LEDGER_VIEW)
HEADER = "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version\n"


def run(*args, check=True):
    return subprocess.run(
        [str(HELPER), *map(str, args)], check=check, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, stdout=subprocess.DEVNULL)


def manifest(path, *, state="reserved", go="0", cost="", status="", terminal="", ticket="T-123"):
    values = {
        "run_id": path.stem,
        "phase": "reserved" if state == "reserved" else "completed",
        "accounting_schema": "1",
        "accounting_state": state,
        "reserved_usd": "2.00",
        "go_issued": go,
        "started_at": "2026-07-15T12:00:00Z",
        "terminal_at": terminal,
        "prompt_version": "3",
        "turns": "2",
        "effective_cost": cost,
        "exit_status": status,
        "cost_basis": "provider_reported" if cost else "",
        "ticket": ticket,
        "role": "planner",
        "adapter": "codex",
        "provider_family": "openai",
        "model_id": "gpt-test",
        "selection_reason": "primary_ready",
        "adapter_version": "test",
    }
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))


class LedgerViewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "product"
        (self.root / "factory" / "runs").mkdir(parents=True)
        (self.root / "factory" / "ledger.csv").write_text(
            HEADER + "2026-07-14,09:00:00,T-100,planner,codex,1,1,0.25,0,old,openai,gpt-test,primary_ready,estimated_tokens,test\n"
        )

    def tearDown(self):
        self.temp.cleanup()

    def refresh(self):
        run("refresh", "--factory-root", self.root)
        with (self.root / "factory" / "runtime-ledger.csv").open() as handle:
            return list(csv.DictReader(handle))

    def test_manifest_reservation_then_terminal_replaces_exact_run(self):
        path = self.root / "factory" / "runs" / "run-1.meta"
        manifest(path)
        rows = self.refresh()
        self.assertEqual([row["cost_usd"] for row in rows], ["0.25", "2.00"])
        manifest(
            path, state="completed", go="1", cost="0.40", status="0",
            terminal="2026-07-15T12:05:00Z",
        )
        rows = self.refresh()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["cost_usd"], "0.40")
        self.assertEqual(rows[-1]["exit_status"], "0")

    def test_legacy_rows_are_not_duplicated_by_refresh(self):
        ledger = self.root / "factory" / "ledger.csv"
        ledger.write_text(HEADER + "2026-07-14,09:00:00,T-100,planner,codex,1,1,0.25,0,,,,,,\n")
        self.assertEqual(len(self.refresh()), 1)
        self.assertEqual(len(self.refresh()), 1)

    def test_pre_go_failure_is_zero_cost(self):
        path = self.root / "factory" / "runs" / "run-void.meta"
        manifest(
            path, state="launch_void", status="4",
            terminal="2026-07-15T12:01:00Z",
        )
        row = self.refresh()[-1]
        self.assertEqual((row["cost_usd"], row["turns"], row["cost_basis"]), ("0", "0", "launch_void"))

    def test_projection_uses_existing_launch_and_ledger_locks(self):
        with LEDGER_VIEW.projection_locks(self.root):
            self.assertTrue((self.root / "factory" / ".launch.lock").is_dir())
            self.assertTrue((self.root / "factory" / ".ledger.lock").is_dir())
        self.assertFalse((self.root / "factory" / ".launch.lock").exists())
        self.assertFalse((self.root / "factory" / ".ledger.lock").exists())

    def test_conflicting_terminal_rows_fail_closed(self):
        path = self.root / "factory" / "runs" / "old.meta"
        manifest(
            path, state="completed", go="1", cost="0.50", status="0",
            terminal="2026-07-15T12:05:00Z",
        )
        result = run("refresh", "--factory-root", self.root, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflicting manifest records", result.stderr)

    def test_projection_is_deterministic_and_refuses_unsettled_runs(self):
        origin = Path(self.temp.name) / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "test")
        (self.root / ".gitignore").write_text("factory/runtime-ledger.csv\nfactory/runs/\n")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "fixture")
        git(self.root, "remote", "add", "origin", str(origin))
        git(self.root, "push", "-qu", "origin", "main")
        worktree = Path(self.temp.name) / "closeout"
        git(self.root, "worktree", "add", "-q", "-b", "chore/t123-closeout", str(worktree), "origin/main")

        path = self.root / "factory" / "runs" / "run-1.meta"
        manifest(
            path, state="completed", go="1", cost="0.40", status="0",
            terminal="2026-07-15T12:05:00Z",
        )
        first = run("project", "--factory-root", self.root, "--workdir", worktree, "--ticket", "T-123")
        payload = json.loads(first.stdout)
        content = (worktree / "factory" / "ledger.csv").read_bytes()
        self.assertEqual(payload["row_count"], 2)
        self.assertEqual(payload["ticket_cost_usd"], 0.4)

        git(worktree, "checkout", "--", "factory/ledger.csv")
        second = run("project", "--factory-root", self.root, "--workdir", worktree, "--ticket", "T-123")
        self.assertEqual(json.loads(second.stdout)["sha256"], payload["sha256"])
        self.assertEqual((worktree / "factory" / "ledger.csv").read_bytes(), content)

        git(worktree, "checkout", "--", "factory/ledger.csv")
        other = self.root / "factory" / "runs" / "other-live.meta"
        manifest(other, ticket="T-999")
        before = (worktree / "factory" / "ledger.csv").read_bytes()
        refused = run(
            "project", "--factory-root", self.root, "--workdir", worktree,
            "--ticket", "T-123", check=False,
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("T-999: other-live.meta", refused.stderr)
        self.assertEqual((worktree / "factory" / "ledger.csv").read_bytes(), before)
        other.unlink()

        git(worktree, "checkout", "--", "factory/ledger.csv")
        manifest(path)
        refused = run(
            "project", "--factory-root", self.root, "--workdir", worktree,
            "--ticket", "T-123", check=False,
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("T-123: run-1.meta", refused.stderr)

        path.write_text(
            "run_id=run-1\nphase=resolved\naccounting_schema=\nticket=T-123\n"
        )
        unresolved = run(
            "project", "--factory-root", self.root, "--workdir", worktree,
            "--ticket", "T-123", check=False,
        )
        self.assertNotEqual(unresolved.returncode, 0)
        self.assertIn("T-123: run-1.meta", unresolved.stderr)

        path.unlink()
        legacy = self.root / "factory" / "runs" / "legacy-1.meta"
        legacy.write_text(
            "run_id=legacy-1\nphase=completed\naccounting_schema=\nticket=T-123\n"
        )
        unaccounted = run(
            "project", "--factory-root", self.root, "--workdir", worktree,
            "--ticket", "T-123", check=False,
        )
        self.assertNotEqual(unaccounted.returncode, 0)
        self.assertIn("T-123: legacy-1.meta", unaccounted.stderr)

        legacy.write_text(
            "run_id=old\nphase=completed\naccounting_schema=\nticket=T-999\n"
        )
        cross_ticket = run(
            "project", "--factory-root", self.root, "--workdir", worktree,
            "--ticket", "T-123", check=False,
        )
        self.assertNotEqual(cross_ticket.returncode, 0)
        self.assertIn("T-999: legacy-1.meta", cross_ticket.stderr)

        legacy.unlink()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        git(worktree, "rm", "-qr", "factory")
        (worktree / "factory").symlink_to(outside, target_is_directory=True)
        git(worktree, "add", "factory")
        git(worktree, "commit", "-qm", "replace factory with symlink")
        escaped = run(
            "project", "--factory-root", self.root, "--workdir", worktree,
            "--ticket", "T-123", check=False,
        )
        self.assertNotEqual(escaped.returncode, 0)
        self.assertIn("factory directory must be a real directory", escaped.stderr)
        self.assertFalse((outside / "ledger.csv").exists())


if __name__ == "__main__":
    unittest.main()
