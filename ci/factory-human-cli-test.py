#!/usr/bin/env python3
"""Human CLI contract: preferences select; the sealed launcher authorizes."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = ROOT / "scripts" / "factory-cli.py"
SPEC = importlib.util.spec_from_file_location("factory_cli", CLI_PATH)
CLI = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(CLI)


class FactoryHumanCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.projects = self.root / "projects"
        self.projects.mkdir(mode=0o700)
        for project in ("alpha", "q26"):
            directory = self.projects / project
            directory.mkdir(mode=0o700)
            active = directory / "active.json"
            active.write_text(
                json.dumps({"project": project}) + "\n", encoding="utf-8"
            )
            active.chmod(0o600)
        self.selection = self.root / "current-project"
        self.snapshots = self.root / "snapshots.json"
        self.log = self.root / "launcher.log"
        self.values = {
            "alpha": {
                "doctor": {
                    "checks": {
                        "controller": {"status": "ok"},
                        "isolated_provider": {
                            "active_attempts": 0,
                            "unknown_workers": 0,
                        },
                        "runtime": {"max_concurrent_tickets": 3},
                    },
                    "overall_status": "ok",
                    "schema": "nysa.software-factory.doctor/v2",
                },
                "workflow": {
                    "label": "Nysa",
                    "mode": "production",
                    "project": "alpha",
                    "schema": "factory-operator-workflow/v1",
                    "tickets": [
                        {
                            "depends_on": ["T-99"],
                            "priority": "high",
                            "state": "Backlog",
                            "ticket": "T-10",
                            "title": "Blocked admin controls",
                        },
                        {
                            "depends_on": [],
                            "priority": "high",
                            "state": "Backlog",
                            "ticket": "T-2",
                            "title": "Ship invoice export",
                        },
                        {
                            "depends_on": [],
                            "priority": "urgent",
                            "state": "Backlog",
                            "ticket": "T-4",
                            "title": "Fix \u001b[31msecurity\r\nnow",
                        },
                        {
                            "depends_on": [],
                            "priority": "normal",
                            "state": "Awaiting Approval",
                            "ticket": "T-8",
                            "title": "Approve account recovery",
                        },
                        {
                            "depends_on": [],
                            "priority": "urgent",
                            "state": "Done",
                            "ticket": "T-7",
                            "title": "Already shipped",
                        },
                    ],
                },
            },
            "q26": {
                "doctor": {
                    "checks": {},
                    "overall_status": "ok",
                    "schema": "nysa.software-factory.doctor/v2",
                },
                "workflow": {
                    "label": "Nysa",
                    "mode": "qualification",
                    "project": "q26",
                    "schema": "factory-operator-workflow/v1",
                    "tickets": [
                        {
                            "depends_on": [],
                            "priority": "high",
                            "state": "Awaiting Approval",
                            "ticket": f"T-{number}",
                            "title": title,
                        }
                        for number, title in (
                            (21, "Add invoice export"),
                            (22, "Fix account recovery"),
                            (23, "Improve onboarding"),
                        )
                    ],
                },
            },
        }
        self.write_snapshots()
        self.launcher = self.root / "factory-launch"
        self.launcher.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            f"snapshots = pathlib.Path({str(self.snapshots)!r})\n"
            f"log = pathlib.Path({str(self.log)!r})\n"
            "args = sys.argv[1:]\n"
            "with log.open('a', encoding='utf-8') as handle:\n"
            "    handle.write(json.dumps(args) + '\\n')\n"
            "values = json.loads(snapshots.read_text(encoding='utf-8'))\n"
            "project, command = args[0], args[1:]\n"
            "if command == ['operator-snapshot', 'workflow', '--json']:\n"
            "    result = values[project]['workflow']\n"
            "elif command == ['doctor', '--json']:\n"
            "    result = values[project]['doctor']\n"
            "elif command == ['qualification-finish', '--json']:\n"
            "    result = {'project': project, 'status': 'green'}\n"
            "elif len(command) == 5 and command[:3] == ['operator', 'ready', '--ticket'] and command[4] == '--json':\n"
            "    result = {'project': project, 'status': 'pass', 'ticket': command[3]}\n"
            "elif len(command) == 5 and command[:3] == ['operator', 'approve', '--ticket'] and command[4] == '--json':\n"
            "    result = {'project': project, 'status': 'pass', 'ticket': command[3]}\n"
            "else:\n"
            "    raise SystemExit('unexpected command: ' + repr(command))\n"
            "print(json.dumps(result))\n",
            encoding="utf-8",
        )
        self.launcher.chmod(0o700)

    def tearDown(self):
        self.temporary.cleanup()

    def write_snapshots(self):
        self.snapshots.write_text(
            json.dumps(self.values, sort_keys=True) + "\n", encoding="utf-8"
        )

    def select(self, project):
        self.selection.write_text(project + "\n", encoding="utf-8")
        self.selection.chmod(0o600)

    def invoke(self, arguments, supplied=""):
        output, error = io.StringIO(), io.StringIO()
        code = CLI.run(
            arguments,
            projects_dir=self.projects,
            launcher=self.launcher,
            selection_file=self.selection,
            stdin=io.StringIO(supplied),
            stdout=output,
            stderr=error,
        )
        return code, output.getvalue(), error.getvalue()

    def invocations(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_use_lists_friendly_projects_without_exposing_lane_slug(self):
        code, output, error = self.invoke(["use"], "2\n")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Nysa · Production", output)
        self.assertIn("Nysa · Qualification · 3 tickets", output)
        self.assertNotIn("q26", output)
        self.assertEqual(self.selection.read_text(encoding="utf-8"), "q26\n")
        self.assertEqual(stat.S_IMODE(self.selection.stat().st_mode), 0o600)

    def test_backlog_is_read_only_ranked_and_includes_exact_id_and_safe_title(self):
        self.select("alpha")
        code, output, error = self.invoke(["backlog"])
        self.assertEqual((code, error), (0, ""))
        self.assertLess(output.index("T-4"), output.index("T-2"))
        self.assertLess(output.index("T-2"), output.index("T-10"))
        self.assertIn("Fix security now", output)
        self.assertNotIn("\x1b", output)
        self.assertNotIn("T-7", output)
        self.assertNotIn("Already shipped", output)
        self.assertEqual(
            self.invocations(),
            [["alpha", "operator-snapshot", "workflow", "--json"]],
        )

    def test_doctor_keeps_its_name_and_summarizes_the_sealed_report(self):
        self.select("alpha")
        code, output, error = self.invoke(["doctor"])
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Doctor passed", output)
        self.assertNotIn("Health", output)
        self.assertEqual(self.invocations(), [["alpha", "doctor", "--json"]])

    def test_bare_command_closes_one_qualification_cohort_without_extra_doctor(self):
        self.select("q26")
        code, output, error = self.invoke([], "1\nyes\n")
        self.assertEqual((code, error), (0, ""))
        for title in ("Add invoice export", "Fix account recovery", "Improve onboarding"):
            self.assertIn(title, output)
        calls = self.invocations()
        self.assertEqual(
            [call for call in calls if call[1:] == ["qualification-finish", "--json"]],
            [["q26", "qualification-finish", "--json"]],
        )
        self.assertFalse(any(call[1:] == ["doctor", "--json"] for call in calls))

    def test_bare_command_approves_exact_selected_production_ticket(self):
        self.select("alpha")
        code, output, error = self.invoke([], "1\nyes\n")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Approve account recovery", output)
        self.assertEqual(
            self.invocations()[-1],
            ["alpha", "operator", "approve", "--ticket", "T-8", "--json"],
        )

    def test_next_recommends_highest_eligible_backlog_ticket_and_can_mark_ready(self):
        self.values["alpha"]["workflow"]["tickets"] = [
            ticket
            for ticket in self.values["alpha"]["workflow"]["tickets"]
            if ticket["state"] != "Awaiting Approval"
        ]
        self.write_snapshots()
        self.select("alpha")
        code, output, error = self.invoke(["next"], "1\nyes\n")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Fix security now", output)
        self.assertEqual(
            self.invocations()[-1],
            ["alpha", "operator", "ready", "--ticket", "T-4", "--json"],
        )

    def test_stale_selection_and_malformed_ticket_refuse_before_mutation(self):
        self.select("alpha")
        (self.projects / "alpha" / "active.json").unlink()
        code, _, error = self.invoke(["backlog"])
        self.assertNotEqual(code, 0)
        self.assertIn("select", error.lower())
        self.assertEqual(self.invocations(), [])

        self.select("q26")
        self.values["q26"]["workflow"]["tickets"][0]["ticket"] = "../../bad"
        self.write_snapshots()
        code, _, error = self.invoke([], "1\nyes\n")
        self.assertNotEqual(code, 0)
        self.assertIn("ticket", error.lower())
        self.assertFalse(
            any(call[1:] == ["qualification-finish", "--json"] for call in self.invocations())
        )

    def test_selected_project_preference_cannot_be_a_symlink(self):
        target = self.root / "selection-target"
        target.write_text("alpha\n", encoding="utf-8")
        target.chmod(0o600)
        self.selection.symlink_to(target)
        code, _, error = self.invoke(["backlog"])
        self.assertNotEqual(code, 0)
        self.assertIn("selection", error.lower())
        self.assertEqual(self.invocations(), [])


if __name__ == "__main__":
    unittest.main()
