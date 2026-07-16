#!/usr/bin/env python3
"""Dependency-free regression tests for scripts/linear-sync.py."""

import importlib.util
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("linear_sync", ROOT / "scripts/linear-sync.py")
LINEAR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LINEAR)


class FakeLinear:
    def __init__(self):
        self.calls = []
        self.issues = {}
        self.projects = {}
        self.views = {}
        self.counter = 0

    def __call__(self, _key, query, variables=None):
        variables = variables or {}
        self.calls.append((query, variables))
        if "teams {" in query:
            return {"teams": {"nodes": [{"id": "team-1", "name": "Software Factory", "key": "SF"}]}}
        if "team(id:" in query:
            return {
                "team": {
                    "states": {"nodes": []},
                    "labels": {"nodes": []},
                    "templates": {"nodes": []},
                }
            }
        if "workflowStateCreate" in query:
            name = variables["input"]["name"]
            return {"workflowStateCreate": {"workflowState": {
                "id": f"state-{name.lower().replace(' ', '-')}",
                "name": name,
                "type": variables["input"]["type"],
            }}}
        if "issueLabelCreate" in query:
            name = variables["input"]["name"]
            return {"issueLabelCreate": {"issueLabel": {"id": f"label-{name}", "name": name}}}
        if "templateCreate" in query:
            item = variables["input"]
            return {"templateCreate": {"template": {
                "id": "template-factory",
                "name": item["name"],
                "type": item["type"],
            }}}
        if "projectCreate" in query:
            self.counter += 1
            project = {
                "id": f"project-{self.counter}",
                "name": variables["input"]["name"],
                "url": f"https://linear.app/test/project/project-{self.counter}",
            }
            self.projects[project["id"]] = {
                **project,
                "description": variables["input"].get("description"),
                "content": variables["input"].get("content"),
                "targetDate": variables["input"].get("targetDate"),
                "status": {"name": "Planned"},
            }
            return {"projectCreate": {"project": project}}
        if "project(id:" in query:
            return {"project": self.projects.get(variables["id"])}
        if "customViews(" in query:
            return {"customViews": {"nodes": list(self.views.values())}}
        if "customView(id:" in query:
            return {"customView": self.views.get(variables["id"])}
        if "customViewCreate" in query:
            self.counter += 1
            view = {
                "id": f"view-{self.counter}",
                "name": variables["input"]["name"],
                "slugId": f"factory-pipeline-{self.counter}",
            }
            self.views[view["id"]] = view
            return {"customViewCreate": {"customView": view}}
        if "issueCreate" in query:
            self.counter += 1
            data = variables["input"]
            issue_id = f"issue-{self.counter}"
            issue = {
                "id": issue_id,
                "identifier": f"SF-{self.counter}",
                "title": data["title"],
                "description": data["description"],
                "priority": data.get("priority", 0),
                "state": {"id": data.get("stateId"), "name": self.state_name(data.get("stateId"))},
                "project": {"id": data["projectId"]} if data.get("projectId") else None,
                "labels": {"nodes": [{"id": item, "name": item} for item in data.get("labelIds", [])]},
            }
            self.issues[issue_id] = issue
            return {"issueCreate": {"issue": {"id": issue_id, "identifier": issue["identifier"]}}}
        if "issue(id:" in query:
            return {"issue": self.issues[variables["id"]]}
        if "issueUpdate" in query:
            issue = self.issues[variables["id"]]
            data = variables["input"]
            for key in ("title", "description", "priority"):
                if key in data:
                    issue[key] = data[key]
            if "stateId" in data:
                issue["state"] = {"id": data["stateId"], "name": self.state_name(data["stateId"])}
            if "projectId" in data:
                issue["project"] = {"id": data["projectId"]}
            if "labelIds" in data:
                issue["labels"] = {"nodes": [{"id": item, "name": item} for item in data["labelIds"]]}
            return {"issueUpdate": {"success": True}}
        if "commentCreate" in query:
            return {"commentCreate": {"success": True}}
        raise AssertionError(f"Unhandled GraphQL operation: {query}")

    @staticmethod
    def state_name(state_id):
        if not state_id:
            return "Backlog"
        for _key, (name, _kind) in LINEAR.STATES.items():
            if state_id == f"state-{name.lower().replace(' ', '-')}":
                return name
        suffix = state_id.removeprefix("state-")
        return " ".join(word.capitalize() for word in suffix.split("-"))


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    @staticmethod
    def read():
        return b'{"data":{"ok":true}}'


def config():
    return {
        "team_id": "team-1",
        "team_key": "SF",
        "states": {
            state: f"state-{name.lower().replace(' ', '-')}"
            for state, (name, _kind) in LINEAR.STATES.items()
        },
        "labels": {name: f"label-{name}" for name in LINEAR.LABELS},
    }


class LinearSyncTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.factory = self.root / "factory"
        (self.factory / "initiatives").mkdir(parents=True)
        (self.factory / "tickets").mkdir()
        (self.factory / "initiatives" / "I-001.md").write_text(
            "# First initiative\n\nStatus: planned\nTarget-Date: 2026-09-30\n\n"
            "## Summary\n\nDeliver the first outcome.\n"
        )
        (self.factory / "tickets" / "T-001.md").write_text(
            "# T-001 — first ticket\n\nState: Backlog\nInitiative: I-001\n"
            "Priority: none\nRisk class: medium\nExternal: yes\n\n"
            "## Description\n\nBuild it.\n\n## Acceptance criteria\n\n1. It works.\n\n## Log\n"
        )
        (self.factory / "ledger.csv").write_text(
            "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status\n"
        )
        self.map_path = self.factory / "linear-map.json"
        self.mapping = {
            "_config": config(),
            "_sync": {},
            "initiatives": {},
            "tickets": {},
        }
        self.fake = FakeLinear()

    def tearDown(self):
        self.temp.cleanup()

    def reconcile(self, dry=False):
        with patch.object(LINEAR, "gql", self.fake):
            LINEAR.reconcile("key", self.factory, self.mapping, self.map_path, dry=dry)

    def test_project_and_issue_are_created_with_mapping_labels_and_health(self):
        self.reconcile()
        project_id = self.mapping["initiatives"]["I-001"]["project_id"]
        issue_id = self.mapping["tickets"]["T-001"]["issue_id"]
        self.assertEqual(self.fake.issues[issue_id]["project"]["id"], project_id)
        self.assertEqual(
            sorted(item["id"] for item in self.fake.issues[issue_id]["labels"]["nodes"]),
            ["label-external", "label-risk:medium"],
        )
        self.assertTrue(self.mapping["tickets"]["T-001"]["identifier"].startswith("SF-"))
        self.assertIsNone(self.mapping["_sync"]["last_error"])

    def test_allowed_operator_fields_are_ingested_before_push(self):
        self.reconcile()
        before = (self.factory / "tickets" / "T-001.md").read_text()
        issue = self.fake.issues[self.mapping["tickets"]["T-001"]["issue_id"]]
        issue["priority"] = LINEAR.PRIORITIES["high"]
        issue["state"] = {"id": config()["states"]["ready"], "name": "Ready"}
        self.reconcile()
        self.assertEqual((self.factory / "tickets" / "T-001.md").read_text(), before)
        operator = self.mapping["tickets"]["T-001"]["operator"]
        self.assertEqual(operator["state"], "Ready")
        self.assertEqual(operator["priority"], "high")
        self.assertEqual(issue["state"]["name"], "Ready")
        self.reconcile()
        self.assertEqual(self.mapping["tickets"]["T-001"]["operator"]["state"], "Ready")

    def test_legacy_issue_bootstraps_operator_fields_before_pull(self):
        self.reconcile()
        path = self.factory / "tickets" / "T-001.md"
        path.write_text(path.read_text().replace("Priority: none", "Priority: high"))
        entry = self.mapping["tickets"]["T-001"]
        entry["operator_fields_initialized"] = False
        issue = self.fake.issues[entry["issue_id"]]
        issue["priority"] = LINEAR.PRIORITIES["none"]
        self.reconcile()
        self.assertEqual(issue["priority"], LINEAR.PRIORITIES["high"])
        self.assertTrue(entry["operator_fields_initialized"])

    def test_illegal_linear_state_is_restored_from_markdown(self):
        self.reconcile()
        issue = self.fake.issues[self.mapping["tickets"]["T-001"]["issue_id"]]
        issue["state"] = {"id": config()["states"]["review"], "name": "Review"}
        self.reconcile()
        self.assertIn("State: Backlog", (self.factory / "tickets" / "T-001.md").read_text())
        self.assertEqual(issue["state"]["name"], "Backlog")

    def test_approval_transition_records_marker(self):
        self.reconcile()
        path = self.factory / "tickets" / "T-001.md"
        path.write_text(path.read_text().replace("State: Backlog", "State: Awaiting Approval"))
        issue = self.fake.issues[self.mapping["tickets"]["T-001"]["issue_id"]]
        issue["state"] = {"id": config()["states"]["approved"], "name": "Approved"}
        self.reconcile()
        self.assertIn("State: Awaiting Approval", path.read_text())
        operator = self.mapping["tickets"]["T-001"]["operator"]
        self.assertEqual(operator["state"], "Approved")
        self.assertEqual(operator["approval"], "Linear")

    def test_blocked_ticket_resumes_only_to_declared_state(self):
        self.reconcile()
        path = self.factory / "tickets" / "T-001.md"
        path.write_text(
            path.read_text()
            .replace("State: Backlog", "State: Blocked-Escalated")
            .replace("Initiative: I-001", "Resume-State: Building\nInitiative: I-001")
        )
        issue = self.fake.issues[self.mapping["tickets"]["T-001"]["issue_id"]]
        issue["state"] = {"id": config()["states"]["building"], "name": "Building"}
        self.reconcile()
        self.assertIn("State: Blocked-Escalated", path.read_text())
        self.assertEqual(self.mapping["tickets"]["T-001"]["operator"]["state"], "Building")

    def test_blocked_ticket_cannot_resume_to_evidence_sensitive_state(self):
        self.reconcile()
        path = self.factory / "tickets" / "T-001.md"
        path.write_text(
            path.read_text()
            .replace("State: Backlog", "State: Blocked-Escalated")
            .replace("Initiative: I-001", "Resume-State: Awaiting Approval\nInitiative: I-001")
        )
        issue = self.fake.issues[self.mapping["tickets"]["T-001"]["issue_id"]]
        previous = "Awaiting Approval"
        for target in ("Awaiting Approval", "Done"):
            with self.subTest(target=target):
                path.write_text(
                    path.read_text().replace(
                        f"Resume-State: {previous}", f"Resume-State: {target}"
                    )
                )
                issue["state"] = {
                    "id": config()["states"][LINEAR.normalize_state(target)],
                    "name": target,
                }
                self.reconcile()
                self.assertEqual(issue["state"]["name"], "Blocked-Escalated")
                self.assertNotEqual(
                    self.mapping["tickets"]["T-001"].get("operator", {}).get("state"),
                    target,
                )
                previous = target

    def test_linear_project_membership_is_ingested(self):
        self.reconcile()
        (self.factory / "initiatives" / "I-002.md").write_text(
            "# Second initiative\n\nStatus: planned\n\n## Summary\n\nAnother outcome.\n"
        )
        self.reconcile()
        second_project = self.mapping["initiatives"]["I-002"]["project_id"]
        issue = self.fake.issues[self.mapping["tickets"]["T-001"]["issue_id"]]
        issue["project"] = {"id": second_project}
        self.reconcile()
        self.assertIn("Initiative: I-001", (self.factory / "tickets" / "T-001.md").read_text())
        self.assertEqual(self.mapping["tickets"]["T-001"]["operator"]["initiative"], "I-002")

    def test_factory_view_is_created_and_mapped(self):
        path = self.factory / "initiatives" / "I-001.md"
        path.write_text(path.read_text().replace("Target-Date:", "View: factory\nTarget-Date:"))
        self.reconcile()
        entry = self.mapping["initiatives"]["I-001"]
        self.assertTrue(entry["view_id"].startswith("view-"))
        self.assertTrue(entry["view_slug"].startswith("factory-pipeline-"))
        view = self.fake.views[entry["view_id"]]
        self.assertEqual(view["name"], "First initiative — Factory Pipeline")
        create = next(
            variables["input"]
            for query, variables in self.fake.calls
            if "customViewCreate" in query
        )
        self.assertEqual(
            create["filterData"],
            {"project": {"id": {"eq": entry["project_id"]}}},
        )

    def test_factory_can_project_every_owned_stage(self):
        self.reconcile()
        path = self.factory / "tickets" / "T-001.md"
        issue = self.fake.issues[self.mapping["tickets"]["T-001"]["issue_id"]]
        for state in (
            "planning",
            "building",
            "review",
            "awaiting approval",
            "blocked-escalated",
            "done",
        ):
            text = replace_state(path.read_text(), LINEAR.STATES[state][0])
            path.write_text(text)
            self.reconcile()
            self.assertEqual(issue["state"]["name"], LINEAR.STATES[state][0])

    def test_repeated_cycle_is_idempotent(self):
        self.reconcile()
        self.reconcile()
        updates_before = sum("issueUpdate" in query for query, _variables in self.fake.calls)
        self.reconcile()
        updates_after = sum("issueUpdate" in query for query, _variables in self.fake.calls)
        self.assertEqual(updates_before, updates_after)

    def test_non_factory_labels_are_preserved(self):
        self.reconcile()
        issue = self.fake.issues[self.mapping["tickets"]["T-001"]["issue_id"]]
        issue["labels"]["nodes"].append({"id": "label-customer", "name": "customer"})
        self.reconcile()
        self.assertIn("label-customer", [item["id"] for item in issue["labels"]["nodes"]])

    def test_dry_run_changes_neither_files_nor_map(self):
        before_ticket = (self.factory / "tickets" / "T-001.md").read_text()
        before_map = json.dumps(self.mapping, sort_keys=True)
        self.reconcile(dry=True)
        self.assertEqual((self.factory / "tickets" / "T-001.md").read_text(), before_ticket)
        self.assertEqual(json.dumps(self.mapping, sort_keys=True), before_map)
        self.assertFalse(self.map_path.exists())

    def test_lock_contender_does_not_overwrite_map(self):
        self.mapping["tickets"]["T-001"] = {
            "operator": {"state": "Ready", "observed_at": "fresh"}
        }
        LINEAR.save_map(self.map_path, self.mapping)
        before = self.map_path.read_bytes()
        with (self.factory / ".linear-sync.lock").open("w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/linear-sync.py"),
                 "--factory-root", str(self.root)],
                env={**os.environ, "LINEAR_API_KEY": "test"},
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.map_path.read_bytes(), before)

    def test_setup_creates_all_states_and_labels(self):
        mapping = LINEAR.load_map(self.map_path)
        with patch.object(LINEAR, "gql", self.fake):
            LINEAR.setup("key", mapping, self.map_path)
        self.assertEqual(set(mapping["_config"]["states"]), set(LINEAR.STATES))
        self.assertEqual(set(mapping["_config"]["labels"]), set(LINEAR.LABELS))
        self.assertEqual(mapping["_config"]["template_id"], "template-factory")
        positions = [
            variables["input"]["position"]
            for query, variables in self.fake.calls
            if "workflowStateCreate" in query
        ]
        self.assertEqual(positions, list(LINEAR.STATE_POSITIONS.values()))
        colors = [
            variables["input"]["color"]
            for query, variables in self.fake.calls
            if "workflowStateCreate" in query
        ]
        self.assertEqual(colors, list(LINEAR.STATE_COLORS.values()))

    def test_legacy_map_is_migrated_in_memory(self):
        self.map_path.write_text('{"_config": null, "tickets": {}}\n')
        migrated = LINEAR.load_map(self.map_path)
        self.assertEqual(migrated["initiatives"], {})
        self.assertEqual(migrated["_sync"], {})

    def test_committed_ticket_branch_is_projected_without_checkout(self):
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "test"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "factory"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "main ticket"], check=True)
        subprocess.run(["git", "-C", str(self.root), "switch", "-qc", "ticket/T-001"], check=True)
        path = self.factory / "tickets" / "T-001.md"
        path.write_text(path.read_text().replace("Build it.", "Branch-authored contract."))
        subprocess.run(["git", "-C", str(self.root), "commit", "-qam", "ticket contract"], check=True)
        subprocess.run(["git", "-C", str(self.root), "switch", "-q", "main"], check=True)
        text, source = LINEAR.committed_ticket(self.factory, "T-001")
        self.assertIn("Branch-authored contract.", text)
        self.assertEqual(source, "refs/heads/ticket/T-001")
        self.assertIn("Build it.", path.read_text())

    def test_pushed_ticket_branch_projects_without_fetch(self):
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "test"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "factory"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "main ticket"], check=True)
        remote = self.root / ".git" / "test-origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        subprocess.run(["git", "-C", str(self.root), "remote", "add", "origin", str(remote)], check=True)
        subprocess.run(["git", "-C", str(self.root), "switch", "-qc", "ticket/T-001"], check=True)
        path = self.factory / "tickets" / "T-001.md"
        path.write_text(path.read_text().replace("Build it.", "Pushed branch contract."))
        subprocess.run(["git", "-C", str(self.root), "commit", "-qam", "ticket contract"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "push", "-q", "origin", "HEAD:refs/heads/ticket/T-001"],
            check=True,
        )
        self.assertEqual(
            subprocess.check_output(
                ["git", "-C", str(self.root), "rev-parse", "refs/remotes/origin/ticket/T-001"],
                text=True,
            ).strip(),
            subprocess.check_output(
                ["git", "-C", str(self.root), "rev-parse", "refs/heads/ticket/T-001"],
                text=True,
            ).strip(),
        )
        subprocess.run(["git", "-C", str(self.root), "switch", "-q", "main"], check=True)
        text, source = LINEAR.committed_ticket(self.factory, "T-001")
        self.assertIn("Pushed branch contract.", text)
        self.assertEqual(source, "refs/remotes/origin/ticket/T-001")

        subprocess.run(["git", "-C", str(self.root), "switch", "-q", "ticket/T-001"], check=True)
        path.write_text(path.read_text().replace("Pushed branch contract.", "Updated branch contract."))
        subprocess.run(["git", "-C", str(self.root), "commit", "-qam", "updated contract"], check=True)
        subprocess.run(["git", "-C", str(self.root), "switch", "-q", "main"], check=True)
        text, source = LINEAR.committed_ticket(self.factory, "T-001")
        self.assertIn("Pushed branch contract.", text)
        self.assertNotIn("Updated branch contract.", text)
        self.assertEqual(source, "refs/remotes/origin/ticket/T-001")

        subprocess.run(["git", "-C", str(self.root), "switch", "-q", "ticket/T-001"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "push", "-q", "origin", "HEAD:refs/heads/ticket/T-001"],
            check=True,
        )
        subprocess.run(["git", "-C", str(self.root), "switch", "-q", "main"], check=True)
        text, source = LINEAR.committed_ticket(self.factory, "T-001")
        self.assertIn("Updated branch contract.", text)
        self.assertEqual(source, "refs/remotes/origin/ticket/T-001")

    def test_failure_health_preserves_last_success(self):
        self.mapping["_sync"] = {"last_success_at": "2026-07-13T12:00:00+00:00"}
        LINEAR.record_failure(self.map_path, self.mapping, RuntimeError("offline"))
        saved = json.loads(self.map_path.read_text())
        self.assertEqual(saved["_sync"]["last_success_at"], "2026-07-13T12:00:00+00:00")
        self.assertEqual(saved["_sync"]["last_error"], "offline")
        self.assertIn("failed_at", saved["_sync"])

    def test_graphql_retries_rate_limit(self):
        limited = urllib.error.HTTPError(
            LINEAR.API_URL,
            429,
            "rate limited",
            {"Retry-After": "0"},
            None,
        )
        with (
            patch.object(LINEAR.urllib.request, "urlopen", side_effect=[limited, FakeResponse()]) as request,
            patch.object(LINEAR.time, "sleep"),
        ):
            self.assertEqual(LINEAR.gql("key", "{ ok }"), {"ok": True})
        self.assertEqual(request.call_count, 2)


def replace_state(text, state):
    return LINEAR.replace_field(text, "State", state)


if __name__ == "__main__":
    unittest.main(verbosity=2)
