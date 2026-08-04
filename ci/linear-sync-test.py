#!/usr/bin/env python3
"""Dependency-free regression tests for scripts/linear-sync.py."""

import copy
import importlib.util
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
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
        self.comments = []
        self.issues = {}
        self.projects = {}
        self.views = {}
        self.counter = 0
        self.viewer_id = "viewer-1"
        self.viewer_error = False
        self.issue_update_success = True
        self.comment_create_success = True

    def __call__(self, _key, query, variables=None):
        variables = variables or {}
        self.calls.append((query, variables))
        if "viewer {" in query:
            if self.viewer_error:
                raise RuntimeError("viewer lookup failed")
            return {"viewer": {"id": self.viewer_id}}
        if "teams {" in query:
            return {"teams": {"nodes": [{"id": "team-1", "name": "Software Factory", "key": "SF"}]}}
        if "issues(first:" in query:
            return {"team": {"issues": {
                "nodes": [
                    {
                        "description": issue["description"],
                        "id": issue["id"],
                        "identifier": issue["identifier"],
                        "state": {
                            "type": next(
                                kind for _state, (name, kind) in LINEAR.STATES.items()
                                if name == issue["state"]["name"]
                            ),
                        },
                        "title": issue["title"],
                    }
                    for issue in self.issues.values()
                ],
                "pageInfo": {"endCursor": None, "hasNextPage": False},
            }}}
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
                "assignee": {"id": data["assigneeId"]} if data.get("assigneeId") else None,
                "comments": {"nodes": [], "pageInfo": {"hasPreviousPage": False, "startCursor": None}},
                "updatedAt": "2026-08-01T00:00:00Z",
            }
            self.issues[issue_id] = issue
            return {"issueCreate": {"issue": {"id": issue_id, "identifier": issue["identifier"]}}}
        if "issue(id:" in query:
            return {"issue": self.issues[variables["id"]]}
        if "issueUpdate" in query:
            if not self.issue_update_success:
                return {"issueUpdate": {"success": False}}
            issue = self.issues[variables["id"]]
            data = variables["input"]
            for key in ("title", "description", "priority"):
                if key in data:
                    issue[key] = data[key]
            if "stateId" in data:
                issue["state"] = {"id": data["stateId"], "name": self.state_name(data["stateId"])}
                issue["updatedAt"] = "2026-08-01T00:00:01Z"
            if "projectId" in data:
                issue["project"] = {"id": data["projectId"]}
            if "labelIds" in data:
                issue["labels"] = {"nodes": [{"id": item, "name": item} for item in data["labelIds"]]}
            if "assigneeId" in data:
                issue["assignee"] = {"id": data["assigneeId"]}
            return {"issueUpdate": {"success": True}}
        if "commentCreate" in query:
            if not self.comment_create_success:
                return {"commentCreate": {"success": False}}
            self.comments.append(variables["input"]["body"])
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
        (self.factory / "runs").mkdir()
        (self.factory / "initiatives" / "I-001.md").write_text(
            "# First initiative\n\nStatus: planned\nTarget-Date: 2026-09-30\n\n"
            "## Summary\n\nDeliver the first outcome.\n"
        )
        (self.factory / "tickets" / "T-001.md").write_text(
            "# T-001 — first ticket\n\nState: Backlog\nInitiative: I-001\n"
            "Priority: none\nRisk class: medium\nExternal: yes\nMerge-Policy: manual\n\n"
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
        LINEAR._VIEWER_ID_CACHE.clear()

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

    def test_missing_mapping_adopts_one_existing_issue_and_refuses_duplicates(self):
        self.reconcile()
        issue = next(iter(self.fake.issues.values()))
        issue["priority"] = LINEAR.PRIORITIES["high"]
        issue["state"] = {"id": config()["states"]["ready"], "name": "Ready"}
        self.mapping["tickets"].clear()
        self.reconcile()
        self.assertEqual(self.mapping["tickets"]["T-001"]["issue_id"], issue["id"])
        self.assertEqual(self.mapping["tickets"]["T-001"]["operator"]["priority"], "high")
        self.assertEqual(self.mapping["tickets"]["T-001"]["operator"]["state"], "Ready")
        duplicate = dict(issue)
        duplicate["id"] = "issue-duplicate"
        duplicate["identifier"] = "SF-duplicate"
        self.fake.issues[duplicate["id"]] = duplicate
        self.mapping["tickets"].clear()
        with self.assertRaisesRegex(RuntimeError, "multiple active Factory issues"):
            self.reconcile()

    def test_first_reconciliation_initializes_missing_runs_root(self):
        (self.factory / "runs").rmdir()
        self.reconcile()
        self.assertTrue((self.factory / "runs").is_dir())
        self.assertFalse((self.factory / "runs").is_symlink())

    def test_first_reconciliation_rejects_non_directory_runs_root(self):
        (self.factory / "runs").rmdir()
        (self.factory / "runs").write_text("not a directory\n")
        with self.assertRaisesRegex(RuntimeError, "must be a real directory"):
            self.reconcile()

    def test_first_reconciliation_rejects_symlinked_runs_root(self):
        (self.factory / "runs").rmdir()
        target = self.root / "outside-runs"
        target.mkdir()
        (self.factory / "runs").symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "must be a real directory"):
            self.reconcile()

    def test_dry_reconciliation_does_not_initialize_missing_runs_root(self):
        (self.factory / "runs").rmdir()
        self.reconcile(dry=True)
        self.assertFalse((self.factory / "runs").exists())

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

    def test_operator_can_cancel_a_backlog_ticket(self):
        self.reconcile()
        issue = self.fake.issues[self.mapping["tickets"]["T-001"]["issue_id"]]
        issue["state"] = {"id": config()["states"]["canceled"], "name": "Canceled"}
        self.reconcile()
        self.assertEqual(
            self.mapping["tickets"]["T-001"]["operator"]["state"], "Canceled"
        )
        self.assertEqual(issue["state"]["name"], "Canceled")

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

    def test_protected_auto_merge_policy_advances_linear_approval(self):
        self.reconcile()
        path = self.factory / "tickets" / "T-001.md"
        path.write_text(
            path.read_text()
            .replace("State: Backlog", "State: Awaiting Approval")
            .replace("Merge-Policy: manual", "Merge-Policy: auto")
        )
        issue = self.fake.issues[self.mapping["tickets"]["T-001"]["issue_id"]]
        with patch.object(LINEAR, "protected_merge_policy", return_value="auto"):
            self.reconcile()
            self.assertEqual(issue["state"]["name"], "Approved")
            self.reconcile()
        operator = self.mapping["tickets"]["T-001"]["operator"]
        self.assertEqual(operator["state"], "Approved")
        self.assertEqual(operator["approval"], "Linear")

    def test_unprotected_auto_merge_policy_waits_for_operator(self):
        self.reconcile()
        path = self.factory / "tickets" / "T-001.md"
        path.write_text(
            path.read_text()
            .replace("State: Backlog", "State: Awaiting Approval")
            .replace("Merge-Policy: manual", "Merge-Policy: auto")
        )
        issue = self.fake.issues[self.mapping["tickets"]["T-001"]["issue_id"]]
        with patch.object(LINEAR, "protected_merge_policy", return_value="manual"):
            self.reconcile()
        self.assertEqual(issue["state"]["name"], "Awaiting Approval")

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
        issue["updatedAt"] = "2026-08-01T00:00:00Z"
        self.reconcile()
        self.assertIn("State: Blocked-Escalated", path.read_text())
        self.assertEqual(issue["state"]["name"], "Blocked-Escalated")
        self.assertNotIn("state", self.mapping["tickets"]["T-001"]["operator"])

        self.reconcile()
        entry = self.mapping["tickets"]["T-001"]
        operator = entry["operator"]
        self.assertEqual(
            entry["blocked_remote_updated_at"], "2026-08-01T00:00:01Z"
        )
        issue["state"] = {
            "id": config()["states"]["building"], "name": "Building"
        }
        issue["updatedAt"] = "2026-08-01T00:00:02Z"
        self.reconcile()
        self.assertEqual(self.mapping["tickets"]["T-001"]["operator"]["state"], "Building")

        path.write_text(
            path.read_text() + "\nNew blocker under the same coarse state.\n"
        )
        self.reconcile()
        entry = self.mapping["tickets"]["T-001"]
        self.assertNotIn("state", entry["operator"])
        self.assertEqual(issue["state"]["name"], "Blocked-Escalated")

        issue["updatedAt"] = "2026-08-01T00:00:03Z"
        self.reconcile()
        entry = self.mapping["tickets"]["T-001"]
        self.assertEqual(
            entry["blocked_remote_updated_at"], "2026-08-01T00:00:03Z"
        )
        issue["state"] = {
            "id": config()["states"]["building"], "name": "Building"
        }
        issue["updatedAt"] = "2026-08-01T00:00:04Z"
        self.reconcile()
        self.assertEqual(self.mapping["tickets"]["T-001"]["operator"]["state"], "Building")

    def test_rejected_unblock_is_reported_once_in_health_and_linear(self):
        self.reconcile()
        path = self.factory / "tickets" / "T-001.md"
        path.write_text(
            path.read_text()
            .replace("State: Backlog", "State: Blocked-Escalated")
            .replace(
                "Initiative: I-001", "Resume-State: Review\nInitiative: I-001"
            )
        )
        issue = self.fake.issues[self.mapping["tickets"]["T-001"]["issue_id"]]

        self.reconcile()
        self.reconcile()
        issue["state"] = {"id": config()["states"]["ready"], "name": "Ready"}
        issue["updatedAt"] = "2026-08-01T00:00:02Z"
        self.reconcile()

        rejection = self.mapping["_sync"]["last_rejected"]
        self.assertEqual(rejection["ticket"], "T-001")
        self.assertEqual(rejection["local_state"], "blocked-escalated")
        self.assertEqual(rejection["remote_state"], "ready")
        self.assertEqual(rejection["required_state"], "review")
        self.assertEqual(
            self.mapping["tickets"]["T-001"]["operator_rejection"], rejection
        )
        self.assertEqual(len(self.fake.comments), 1)
        self.assertIn("Resume-State: Review", self.fake.comments[0])
        self.assertIn("OPERATOR RESUME RECEIPT", self.fake.comments[0])

        self.reconcile()
        self.assertEqual(len(self.fake.comments), 1)
        saved = json.loads(self.map_path.read_text())
        self.assertEqual(saved["_sync"]["last_rejected"], rejection)

        issue["state"] = {"id": config()["states"]["ready"], "name": "Ready"}
        issue["updatedAt"] = "2026-08-01T00:00:03Z"
        self.fake.comment_create_success = False
        with self.assertRaisesRegex(RuntimeError, "commentCreate did not succeed"):
            self.reconcile()
        self.assertEqual(
            self.mapping["_sync"]["last_rejected"]["rejection_sha256"],
            rejection["rejection_sha256"],
        )

    def test_repeated_block_without_overlay_requires_a_fresh_remote_baseline(self):
        self.reconcile()
        path = self.factory / "tickets" / "T-001.md"
        path.write_text(
            path.read_text()
            .replace("State: Backlog", "State: Blocked-Escalated")
            .replace("Initiative: I-001", "Resume-State: Building\nInitiative: I-001")
        )
        issue = self.fake.issues[self.mapping["tickets"]["T-001"]["issue_id"]]

        self.reconcile()
        self.reconcile()
        entry = self.mapping["tickets"]["T-001"]
        self.assertNotIn("state", entry["operator"])
        self.assertEqual(entry["blocked_remote_updated_at"], "2026-08-01T00:00:01Z")

        issue["description"] = re.sub(
            r"(?m)^(\d+[.)] )", r" \1", issue["description"]
        )
        before_updated_at = issue["updatedAt"]
        before_baseline = entry["blocked_remote_updated_at"]
        updates_before = sum(
            "issueUpdate" in query for query, _variables in self.fake.calls
        )
        self.reconcile()
        self.assertEqual(
            updates_before,
            sum("issueUpdate" in query for query, _variables in self.fake.calls),
        )
        self.assertEqual(issue["updatedAt"], before_updated_at)
        self.assertEqual(entry["blocked_remote_updated_at"], before_baseline)

        issue["state"] = {"id": config()["states"]["building"], "name": "Building"}
        issue["updatedAt"] = "2026-08-01T00:00:02Z"
        path.write_text(path.read_text() + "\nNew blocker with no ingested overlay.\n")
        self.reconcile()
        self.assertEqual(issue["state"]["name"], "Blocked-Escalated")
        self.assertNotIn("state", entry["operator"])
        self.assertNotIn("blocked_remote_updated_at", entry)

        issue["updatedAt"] = "2026-08-01T00:00:03Z"
        self.reconcile()
        self.assertEqual(entry["blocked_remote_updated_at"], "2026-08-01T00:00:03Z")
        issue["state"] = {"id": config()["states"]["building"], "name": "Building"}
        issue["updatedAt"] = "2026-08-01T00:00:04Z"
        self.reconcile()
        self.assertEqual(entry["operator"]["state"], "Building")

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
        for target in ("Awaiting Approval", "Approved", "Done"):
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

    def test_blocked_escalated_ticket_is_assigned_to_viewer(self):
        self.reconcile()
        path = self.factory / "tickets" / "T-001.md"
        path.write_text(path.read_text().replace("State: Backlog", "State: Blocked-Escalated"))
        issue = self.fake.issues[self.mapping["tickets"]["T-001"]["issue_id"]]

        self.reconcile()
        update = next(
            variables["input"]
            for query, variables in reversed(self.fake.calls)
            if "issueUpdate" in query
        )
        self.assertEqual(update["assigneeId"], "viewer-1")
        self.assertEqual(issue["assignee"], {"id": "viewer-1"})

        updates_before = sum("issueUpdate" in query for query, _variables in self.fake.calls)
        self.reconcile()
        updates_after = sum("issueUpdate" in query for query, _variables in self.fake.calls)
        self.assertEqual(updates_before, updates_after)

    def test_awaiting_approval_ticket_is_assigned_to_viewer(self):
        self.reconcile()
        path = self.factory / "tickets" / "T-001.md"
        path.write_text(path.read_text().replace("State: Backlog", "State: Awaiting Approval"))
        self.reconcile()
        issue = self.fake.issues[self.mapping["tickets"]["T-001"]["issue_id"]]
        self.assertEqual(issue["assignee"], {"id": "viewer-1"})

    def test_viewer_lookup_failure_does_not_block_state_patch(self):
        self.reconcile()
        path = self.factory / "tickets" / "T-001.md"
        path.write_text(path.read_text().replace("State: Backlog", "State: Blocked-Escalated"))
        self.fake.viewer_error = True
        LINEAR._VIEWER_ID_CACHE.clear()

        self.reconcile()
        issue = self.fake.issues[self.mapping["tickets"]["T-001"]["issue_id"]]
        self.assertEqual(issue["state"]["name"], "Blocked-Escalated")
        self.assertIsNone(issue.get("assignee"))
        update = next(
            variables["input"]
            for query, variables in reversed(self.fake.calls)
            if "issueUpdate" in query
        )
        self.assertNotIn("assigneeId", update)

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

        issue["project"] = None
        self.reconcile()
        self.reconcile()
        self.assertIsNone(self.mapping["tickets"]["T-001"]["operator"]["initiative"])
        self.assertIsNone(issue["project"])
        self.assertIn("Initiative: I-001", (self.factory / "tickets" / "T-001.md").read_text())

        issue["project"] = {"id": "external-project"}
        self.reconcile()
        self.reconcile()
        self.assertIsNone(self.mapping["tickets"]["T-001"]["operator"]["initiative"])
        self.assertEqual(issue["project"], {"id": "external-project"})

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
            "canceled",
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

    def test_linear_link_wrappers_do_not_trigger_description_rewrite(self):
        path = self.factory / "tickets" / "T-001.md"
        path.write_text(path.read_text().replace("Build it.", "See [spec](https://example.com/spec)."))
        self.reconcile()
        issue = self.fake.issues[self.mapping["tickets"]["T-001"]["issue_id"]]
        issue["description"] = issue["description"].replace(
            "](https://example.com/spec)", "](<https://example.com/spec>)"
        )
        updates_before = sum("issueUpdate" in query for query, _variables in self.fake.calls)
        self.reconcile()
        updates_after = sum("issueUpdate" in query for query, _variables in self.fake.calls)
        self.assertEqual(updates_before, updates_after)

    def test_linear_ordered_list_round_trip_does_not_trigger_description_rewrite(self):
        path = self.factory / "tickets" / "T-001.md"
        path.write_text(
            path.read_text().replace(
                "1. It works.",
                "1. The exact first clause remains semantically joined\n"
                "   to the second clause.\n"
                "5. The deliberately numbered second clause remains equivalent.",
            )
        )
        self.reconcile()
        issue = self.fake.issues[self.mapping["tickets"]["T-001"]["issue_id"]]
        issue["description"] = re.sub(
            r"(?m)^(\d+)([.)] )",
            lambda match: (
                f" {2 if match.group(1) == '5' else match.group(1)}{match.group(2)}"
            ),
            issue["description"],
        ).replace(
            "\n   to the second clause.", "\n    to the second clause."
        )
        updates_before = sum(
            "issueUpdate" in query for query, _variables in self.fake.calls
        )
        self.reconcile()
        updates_after = sum(
            "issueUpdate" in query for query, _variables in self.fake.calls
        )
        self.assertEqual(updates_before, updates_after)
        self.assertNotEqual(
            LINEAR.normalize_md("1. Parent\n   - Nested child"),
            LINEAR.normalize_md("1. Parent - Nested child"),
        )
        issue["description"] = issue["description"].replace(
            "exact first clause", "meaningfully changed clause"
        )
        updates_before = sum(
            "issueUpdate" in query for query, _variables in self.fake.calls
        )
        self.reconcile()
        self.assertEqual(
            sum("issueUpdate" in query for query, _variables in self.fake.calls),
            updates_before + 1,
        )

    def test_linear_fence_boundary_round_trip_does_not_trigger_description_rewrite(self):
        path = self.factory / "tickets" / "T-001.md"
        path.write_text(
            path.read_text().replace("Build it.", "Records orders\n```\nfixture")
        )
        self.reconcile()
        issue = self.fake.issues[self.mapping["tickets"]["T-001"]["issue_id"]]
        issue["description"] = issue["description"].replace(
            "Records orders\n```", "Records orders\n\n```"
        ) + "\n```"
        updates_before = sum(
            "issueUpdate" in query for query, _variables in self.fake.calls
        )
        self.reconcile()
        self.assertEqual(
            updates_before,
            sum("issueUpdate" in query for query, _variables in self.fake.calls),
        )
        self.assertNotEqual(
            LINEAR.normalize_md("Records orders\n```\nfixture"),
            LINEAR.normalize_md("Records orders\n```\nchanged"),
        )

    def test_inline_delimited_branch_is_rendered_once(self):
        path = self.factory / "tickets" / "T-001.md"
        path.write_text(
            path.read_text().replace(
                "Initiative: I-001", "Initiative: I-001\nBranch: `ticket/T-001`"
            )
        )
        self.reconcile()
        issue = self.fake.issues[self.mapping["tickets"]["T-001"]["issue_id"]]
        self.assertIn("**Branch:** `ticket/T-001`", issue["description"])
        self.assertNotIn("``ticket/T-001``", issue["description"])

    def test_review_bundle_posts_once_after_successful_narrator(self):
        self.reconcile()
        path = self.factory / "tickets" / "T-001.md"
        path.write_text(path.read_text().replace("State: Backlog", "State: Review"))
        (self.factory / "tickets" / "T-001-bundle.md").write_text("Verified bundle\n")

        self.reconcile()
        self.assertIsNone(self.mapping["tickets"]["T-001"]["bundle_digest"])
        self.assertFalse(any(body.startswith("**Evidence bundle**") for body in self.fake.comments))

        with (self.factory / "ledger.csv").open("a") as handle:
            handle.write("2026-07-15,12:00:00,T-001,narrator,codex,3,1,0.10,0\n")
        self.reconcile()
        evidence = [
            body for body in self.fake.comments if body.startswith("**Evidence bundle**")
        ]
        self.assertEqual(evidence, ["**Evidence bundle**\n\nVerified bundle\n"])
        first_digest = self.mapping["tickets"]["T-001"]["bundle_digest"]
        self.assertRegex(first_digest, r"^[0-9a-f]{64}$")

        self.reconcile()
        self.assertEqual(
            len([body for body in self.fake.comments if body.startswith("**Evidence bundle**")]),
            1,
        )

        (self.factory / "tickets" / "T-001-bundle.md").write_text("Updated bundle\n")
        self.reconcile()
        self.assertNotEqual(self.mapping["tickets"]["T-001"]["bundle_digest"], first_digest)
        self.assertEqual(
            len([body for body in self.fake.comments if body.startswith("**Evidence bundle**")]),
            2,
        )

    def test_legacy_bundle_boolean_causes_one_corrective_post(self):
        self.reconcile()
        path = self.factory / "tickets" / "T-001.md"
        path.write_text(path.read_text().replace("State: Backlog", "State: Awaiting Approval"))
        (self.factory / "tickets" / "T-001-bundle.md").write_text("Verified bundle\n")
        entry = self.mapping["tickets"]["T-001"]
        entry.pop("bundle_digest", None)
        entry["bundle_posted"] = True
        self.reconcile()
        self.assertNotIn("bundle_posted", entry)
        self.assertRegex(entry["bundle_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(len(self.fake.comments), 1)

    def test_failed_comments_do_not_advance_log_or_bundle_markers(self):
        self.reconcile()
        path = self.factory / "tickets" / "T-001.md"
        path.write_text(
            path.read_text()
            .replace("State: Backlog", "State: Awaiting Approval")
            .replace("## Log\n", "## Log\n\n- needs attention\n")
        )
        (self.factory / "tickets" / "T-001-bundle.md").write_text("Verified bundle\n")
        self.fake.comment_create_success = False
        with self.assertRaisesRegex(RuntimeError, "commentCreate did not succeed"):
            self.reconcile()
        entry = self.mapping["tickets"]["T-001"]
        self.assertEqual(entry["log_cursor"], 0)
        self.assertIsNone(entry["bundle_digest"])

    def test_failed_issue_update_is_reported(self):
        self.reconcile()
        path = self.factory / "tickets" / "T-001.md"
        path.write_text(path.read_text().replace("State: Backlog", "State: Review"))
        self.fake.issue_update_success = False
        with self.assertRaisesRegex(RuntimeError, "issueUpdate did not succeed"):
            self.reconcile()

    def test_non_factory_labels_are_preserved(self):
        self.reconcile()
        issue = self.fake.issues[self.mapping["tickets"]["T-001"]["issue_id"]]
        issue["labels"]["nodes"].append({"id": "label-customer", "name": "customer"})
        self.reconcile()
        self.assertIn("label-customer", [item["id"] for item in issue["labels"]["nodes"]])

    def test_dry_run_changes_neither_files_nor_map(self):
        before_ticket = (self.factory / "tickets" / "T-001.md").read_text()
        before_map = json.dumps(self.mapping, sort_keys=True)
        runtime = self.factory / "runtime-ledger.csv"
        runtime.write_bytes(b"dry-run sentinel\n")
        self.reconcile(dry=True)
        self.assertEqual((self.factory / "tickets" / "T-001.md").read_text(), before_ticket)
        self.assertEqual(json.dumps(self.mapping, sort_keys=True), before_map)
        self.assertFalse(self.map_path.exists())
        self.assertEqual(runtime.read_bytes(), b"dry-run sentinel\n")

    def test_lock_contender_does_not_overwrite_map(self):
        self.mapping["tickets"]["T-001"] = {
            "operator": {"state": "Ready", "observed_at": "fresh"}
        }
        LINEAR.save_map(self.map_path, self.mapping)
        before = self.map_path.read_bytes()
        with (self.factory / ".linear-sync-cycle.lock").open("w") as handle:
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

    def test_ticket_clear_survives_stale_full_board_save(self):
        operator = {"state": "Ready", "observed_at": "fresh"}
        self.mapping["tickets"]["T-001"] = {"operator": operator}
        LINEAR.save_map(self.map_path, self.mapping)
        digest = LINEAR.operator_version(operator)
        intents = self.factory / ".linear-operator-clears"
        intents.mkdir(mode=0o700)
        (intents / f"T-001-{digest}.json").write_text(json.dumps({
            "operator_version": digest,
            "schema": "linear-operator-clear/v1",
            "ticket": "T-001",
        }))

        LINEAR.save_map(self.map_path, json.loads(json.dumps(self.mapping)))
        self.assertNotIn(
            "operator", json.loads(self.map_path.read_text())["tickets"]["T-001"]
        )
        LINEAR.retire_operator_clears(self.map_path)
        self.assertFalse(any(intents.iterdir()))

    def test_full_board_cycle_does_not_hold_ticket_map_lock(self):
        started = time.monotonic()
        with LINEAR.sync_lock(self.factory):
            LINEAR.save_map(self.map_path, self.mapping)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_exact_ticket_sync_ingests_mapped_operator_without_full_board_reads(self):
        self.reconcile()
        issue = self.fake.issues[self.mapping["tickets"]["T-001"]["issue_id"]]
        issue["state"] = {"id": config()["states"]["ready"], "name": "Ready"}
        issue["updatedAt"] = "2026-08-01T00:00:01Z"
        before_health = json.loads(self.map_path.read_text())["_sync"]
        self.fake.calls.clear()

        with patch.object(LINEAR, "gql", self.fake):
            LINEAR.sync_ticket_operator(
                "key", self.factory, self.map_path, "T-001"
            )

        saved = json.loads(self.map_path.read_text())
        self.assertEqual(saved["tickets"]["T-001"]["operator"]["state"], "Ready")
        self.assertEqual(saved["_sync"], before_health)
        self.assertEqual(
            sum("issue(id:" in query for query, _variables in self.fake.calls), 1
        )
        self.assertFalse(any(
            "issues(first:" in query
            or "project(id:" in query
            or "viewer {" in query
            for query, _variables in self.fake.calls
        ))

    def test_exact_ticket_sync_persists_rejected_unblock_health(self):
        self.reconcile()
        path = self.factory / "tickets" / "T-001.md"
        path.write_text(
            path.read_text()
            .replace("State: Backlog", "State: Blocked-Escalated")
            .replace(
                "Initiative: I-001", "Resume-State: Review\nInitiative: I-001"
            )
        )
        self.reconcile()
        self.reconcile()
        stale = LINEAR.load_map(self.map_path)
        issue = self.fake.issues[self.mapping["tickets"]["T-001"]["issue_id"]]
        issue["state"] = {"id": config()["states"]["ready"], "name": "Ready"}
        issue["updatedAt"] = "2026-08-01T00:00:02Z"

        with patch.object(LINEAR, "gql", self.fake):
            LINEAR.sync_ticket_operator(
                "key", self.factory, self.map_path, "T-001"
            )
        LINEAR.save_map(self.map_path, stale)

        saved = json.loads(self.map_path.read_text())
        rejection = saved["tickets"]["T-001"]["operator_rejection"]
        self.assertEqual(saved["_sync"]["last_rejected"], rejection)
        self.assertEqual(rejection["required_state"], "review")

    def test_exact_ticket_sync_refuses_unmapped_ticket_without_network_or_write(self):
        self.reconcile()
        mapping = LINEAR.load_map(self.map_path)
        mapping["tickets"].clear()
        LINEAR.save_map(self.map_path, mapping)
        before = self.map_path.read_bytes()
        self.fake.calls.clear()

        with (
            patch.object(LINEAR, "gql", self.fake),
            self.assertRaisesRegex(RuntimeError, "mapped initialized Linear issue"),
        ):
            LINEAR.sync_ticket_operator(
                "key", self.factory, self.map_path, "T-001"
            )

        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.map_path.read_bytes(), before)

    def test_exact_ticket_sync_survives_overlapping_stale_full_cycle_save(self):
        self.reconcile()
        stale = LINEAR.load_map(self.map_path)
        issue = self.fake.issues[self.mapping["tickets"]["T-001"]["issue_id"]]
        issue["state"] = {"id": config()["states"]["ready"], "name": "Ready"}
        issue["updatedAt"] = "2026-08-01T00:00:01Z"

        with LINEAR.sync_lock(self.factory):
            with patch.object(LINEAR, "gql", self.fake):
                LINEAR.sync_ticket_operator(
                    "key", self.factory, self.map_path, "T-001"
                )
            LINEAR.save_map(self.map_path, stale)

        saved = json.loads(self.map_path.read_text())
        self.assertEqual(saved["tickets"]["T-001"]["operator"]["state"], "Ready")

    def test_stale_save_never_copies_operator_overlay_across_issue_remap(self):
        self.reconcile()
        stale = LINEAR.load_map(self.map_path)
        remapped = copy.deepcopy(stale)
        entry = remapped["tickets"]["T-001"]
        entry["issue_id"] = "issue-remapped"
        entry["operator"] = {
            "priority": "none",
            "state": "Ready",
            "state_base": "backlog",
            "linear_updated_at": "2026-08-01T00:00:02Z",
            "observed_at": "2026-08-01T00:00:02+00:00",
        }
        LINEAR.save_map(self.map_path, remapped)
        LINEAR.save_map(self.map_path, stale)

        saved = json.loads(self.map_path.read_text())["tickets"]["T-001"]
        self.assertNotEqual(saved.get("operator", {}).get("state"), "Ready")

    def test_exact_ticket_timeout_and_rate_limit_leave_no_partial_overlay(self):
        self.reconcile()
        before = self.map_path.read_bytes()
        for error in (TimeoutError("timed out"), RuntimeError("Linear HTTP 429")):
            with self.subTest(error=str(error)):
                with (
                    patch.object(LINEAR, "fetch_issue", side_effect=error),
                    self.assertRaises(type(error)),
                ):
                    LINEAR.sync_ticket_operator(
                        "key", self.factory, self.map_path, "T-001"
                    )
                self.assertEqual(self.map_path.read_bytes(), before)

    def test_terminal_sync_projects_only_protected_done_and_is_idempotent(self):
        self.reconcile()
        mapping = LINEAR.load_map(self.map_path)
        mapping["tickets"]["T-001"]["operator"] = {
            "approval": "Linear",
            "state": "Approved",
            "state_base": "awaiting approval",
        }
        LINEAR.save_map(self.map_path, mapping)
        done = (self.factory / "tickets/T-001.md").read_text().replace(
            "State: Backlog", "State: Done"
        )

        with (
            patch.object(LINEAR, "committed_ticket", return_value=(
                done, "refs/remotes/origin/main",
            )),
            patch.object(LINEAR, "gql", self.fake),
        ):
            first = LINEAR.sync_ticket_terminal(
                "key", self.factory, self.map_path, "T-001"
            )
            second = LINEAR.sync_ticket_terminal(
                "key", self.factory, self.map_path, "T-001"
            )

        self.assertTrue(first["updated"])
        self.assertFalse(second["updated"])
        self.assertEqual(first["state"], "Done")
        self.assertEqual(sum(
            "issueUpdate" in query for query, _variables in self.fake.calls
        ), 1)
        saved = LINEAR.load_map(self.map_path)["tickets"]["T-001"]
        self.assertEqual(saved["source_ref"], "refs/remotes/origin/main")
        self.assertNotIn("operator", saved)

    def test_terminal_sync_refuses_before_protected_truth_without_network(self):
        self.reconcile()
        before = self.map_path.read_bytes()
        self.fake.calls.clear()
        text = (self.factory / "tickets/T-001.md").read_text().replace(
            "State: Backlog", "State: Done"
        )

        with (
            patch.object(LINEAR, "committed_ticket", return_value=(text, "HEAD")),
            patch.object(LINEAR, "gql", self.fake),
            self.assertRaisesRegex(RuntimeError, "protected terminal ticket"),
        ):
            LINEAR.sync_ticket_terminal(
                "key", self.factory, self.map_path, "T-001"
            )

        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.map_path.read_bytes(), before)

    def test_terminal_sync_refuses_unmapped_ticket_without_network(self):
        self.reconcile()
        mapping = LINEAR.load_map(self.map_path)
        mapping["tickets"].clear()
        LINEAR.save_map(self.map_path, mapping)
        before = self.map_path.read_bytes()
        self.fake.calls.clear()

        with (
            patch.object(LINEAR, "gql", self.fake),
            self.assertRaisesRegex(RuntimeError, "mapped initialized Linear issue"),
        ):
            LINEAR.sync_ticket_terminal(
                "key", self.factory, self.map_path, "T-001"
            )

        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.map_path.read_bytes(), before)

    def test_operator_map_path_honors_only_absolute_launcher_binding(self):
        external = self.root / "operator-map.json"
        with patch.dict(os.environ, {"FACTORY_OPERATOR_MAP": str(external)}):
            self.assertEqual(LINEAR.operator_map_path(self.factory), external)
        with (
            patch.dict(os.environ, {"FACTORY_OPERATOR_MAP": "relative.json"}),
            self.assertRaisesRegex(RuntimeError, "must be absolute"),
        ):
            LINEAR.operator_map_path(self.factory)

    def test_failed_terminal_update_leaves_map_unchanged(self):
        self.reconcile()
        before = self.map_path.read_bytes()
        self.fake.issue_update_success = False
        done = (self.factory / "tickets/T-001.md").read_text().replace(
            "State: Backlog", "State: Done"
        )

        with (
            patch.object(LINEAR, "committed_ticket", return_value=(
                done, "refs/remotes/origin/main",
            )),
            patch.object(LINEAR, "gql", self.fake),
            self.assertRaisesRegex(RuntimeError, "terminal issueUpdate"),
        ):
            LINEAR.sync_ticket_terminal(
                "key", self.factory, self.map_path, "T-001"
            )

        self.assertEqual(self.map_path.read_bytes(), before)

    def test_terminal_sync_rechecks_protected_source_before_mutation(self):
        self.reconcile()
        before = self.map_path.read_bytes()
        done = (self.factory / "tickets/T-001.md").read_text().replace(
            "State: Backlog", "State: Done"
        )
        self.fake.calls.clear()

        with (
            patch.object(LINEAR, "committed_ticket", side_effect=(
                (done, "refs/remotes/origin/main"),
                (done + "\n", "refs/remotes/origin/main"),
            )),
            patch.object(LINEAR, "gql", self.fake),
            self.assertRaisesRegex(RuntimeError, "protected terminal ticket changed"),
        ):
            LINEAR.sync_ticket_terminal(
                "key", self.factory, self.map_path, "T-001"
            )

        self.assertFalse(any(
            "issueUpdate" in query for query, _variables in self.fake.calls
        ))
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
        bundle = self.factory / "tickets" / "T-001-bundle.md"
        bundle.write_text("Committed bundle.\n")
        subprocess.run(["git", "-C", str(self.root), "add", "factory"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "main ticket"], check=True)
        path = self.factory / "tickets" / "T-001.md"
        path.write_text(path.read_text().replace("Build it.", "Dirty checkout contract."))
        text, source = LINEAR.committed_ticket(self.factory, "T-001")
        self.assertIn("Build it.", text)
        self.assertNotIn("Dirty checkout contract.", text)
        self.assertEqual(source, "HEAD")
        bundle.write_text("Dirty bundle.\n")
        bundle_text, bundle_source = LINEAR.committed_factory_file(
            self.factory, "T-001", "T-001-bundle.md"
        )
        self.assertEqual(bundle_text, "Committed bundle.\n")
        self.assertEqual(bundle_source, "HEAD")
        subprocess.run(["git", "-C", str(self.root), "restore", str(path)], check=True)
        subprocess.run(["git", "-C", str(self.root), "restore", str(bundle)], check=True)
        untracked = self.factory / "tickets" / "T-999.md"
        untracked.write_text("# Untracked\n\nState: Ready\n")
        self.assertEqual(LINEAR.committed_ticket(self.factory, "T-999"), (None, None))
        self.reconcile()
        self.assertNotIn("T-999", self.mapping["tickets"])
        untracked.unlink()
        untracked_bundle = self.factory / "tickets" / "T-999-bundle.md"
        untracked_bundle.write_text("Untracked bundle.\n")
        self.assertEqual(
            LINEAR.committed_factory_file(
                self.factory, "T-999", "T-999-bundle.md"
            ),
            (None, None),
        )
        untracked_bundle.unlink()
        subprocess.run(["git", "-C", str(self.root), "switch", "-qc", "ticket/T-001"], check=True)
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

    def test_exact_unconsumed_comment_records_fallback_approval(self):
        approval_hash = "a" * 64
        nonce = "b" * 32
        actual = {
            "comments": {
                "nodes": [
                    {
                        "id": "comment-1",
                        "body": (
                            f"FACTORY MODEL FALLBACK APPROVAL: {approval_hash} "
                            "RUN: run-1 REASON: credits_exhausted "
                            f"NONCE: {nonce}"
                        ),
                        "createdAt": "2026-07-18T12:00:00Z",
                        "updatedAt": "2026-07-18T12:00:01Z",
                        "user": {"id": "operator-1", "name": "Operator"},
                    },
                    {
                        "id": "ignored",
                        "body": "please retry",
                        "createdAt": "2026-07-18T12:01:00Z",
                        "updatedAt": "2026-07-18T12:01:00Z",
                        "user": {"id": "operator-1", "name": "Operator"},
                    },
                ]
            }
        }
        entry = {}
        with patch.object(
            LINEAR, "utc_now", return_value="2026-07-18T12:05:00+00:00"
        ):
            LINEAR.ingest_fallback_approval(actual, entry, False)
        approval = entry["model_fallback_approval"]
        self.assertEqual(approval["approval_hash"], approval_hash)
        self.assertEqual(approval["failed_run_id"], "run-1")
        self.assertEqual(approval["operator_id"], "operator-1")
        self.assertEqual(approval["reason"], "credits_exhausted")
        self.assertEqual(approval["nonce"], nonce)

        actual["comments"]["nodes"].extend([
            {
                "id": "expired-newer",
                "body": (
                    f"FACTORY MODEL FALLBACK APPROVAL: {'c' * 64} "
                    "RUN: run-1 REASON: credits_exhausted "
                    f"NONCE: {'d' * 32}"
                ),
                "createdAt": "2026-07-18T11:00:00Z",
                "updatedAt": "2026-07-18T12:06:00Z",
                "user": {"id": "operator-1", "name": "Operator"},
            },
            {
                "id": "wrong-newer",
                "body": (
                    f"FACTORY MODEL FALLBACK APPROVAL: {'e' * 64} "
                    "RUN: run-1 REASON: credits_exhausted "
                    f"NONCE: {'f' * 32}"
                ),
                "createdAt": "2026-07-18T12:06:00Z",
                "updatedAt": "2026-07-18T12:06:00Z",
                "user": {"id": "operator-1", "name": "Operator"},
            },
        ])
        with patch.object(
            LINEAR, "utc_now", return_value="2026-07-18T12:07:00+00:00"
        ):
            LINEAR.ingest_fallback_approval(actual, entry, False)
        self.assertEqual(
            entry["model_fallback_approval"]["comment_id"],
            "wrong-newer",
        )

        consumed = {"consumed_model_fallback_comment_ids": ["comment-1"]}
        with patch.object(
            LINEAR, "utc_now", return_value="2026-07-18T12:07:00+00:00"
        ):
            LINEAR.ingest_fallback_approval(actual, consumed, False)
        self.assertEqual(
            consumed["model_fallback_approval"]["comment_id"],
            "wrong-newer",
        )

    def test_fetch_issue_paginates_complete_comment_history(self):
        approval_hash = "a" * 64
        nonce = "b" * 32
        issue = {
            "comments": {
                "nodes": [],
                "pageInfo": {"hasPreviousPage": True, "startCursor": "page-1"},
            }
        }
        approval = {
            "id": "comment-2",
            "body": (
                f"FACTORY MODEL FALLBACK APPROVAL: {approval_hash} "
                f"RUN: run-1 REASON: provider_unavailable NONCE: {nonce}"
            ),
            "createdAt": "2026-07-18T12:00:00Z",
            "updatedAt": "2026-07-18T12:00:00Z",
            "user": {"id": "operator-1", "name": "Operator"},
        }
        responses = [
            {"issue": issue},
            {"issue": {"comments": {
                "nodes": [approval],
                "pageInfo": {"hasPreviousPage": False, "startCursor": None},
            }}},
        ]
        with patch.object(LINEAR, "gql", side_effect=responses) as query:
            actual = LINEAR.fetch_issue("key", "issue-1")
        self.assertEqual(query.call_count, 2)
        entry = {}
        with patch.object(LINEAR, "utc_now", return_value="2026-07-18T12:05:00+00:00"):
            LINEAR.ingest_fallback_approval(actual, entry, False)
        self.assertEqual(entry["model_fallback_approval"]["comment_id"], "comment-2")

    def test_fetch_issue_rejects_incomplete_comment_pagination(self):
        response = {"issue": {"comments": {
            "nodes": [],
            "pageInfo": {"hasPreviousPage": True, "startCursor": None},
        }}}
        with (
            patch.object(LINEAR, "gql", return_value=response),
            self.assertRaisesRegex(RuntimeError, "missing cursor"),
        ):
            LINEAR.fetch_issue("key", "issue-1")

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

    def test_graphql_retries_transient_server_failure(self):
        unavailable = urllib.error.HTTPError(
            LINEAR.API_URL,
            503,
            "service unavailable",
            {},
            None,
        )
        with (
            patch.object(
                LINEAR.urllib.request,
                "urlopen",
                side_effect=[unavailable, FakeResponse()],
            ) as request,
            patch.object(LINEAR.time, "sleep") as sleep,
        ):
            self.assertEqual(LINEAR.gql("key", "{ ok }"), {"ok": True})
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_graphql_uses_bounded_backoff_for_malformed_retry_after(self):
        for value, expected in (
            ("not-a-number", 1),
            ("-20", 0),
            ("999999", 30),
        ):
            with self.subTest(retry_after=value):
                limited = urllib.error.HTTPError(
                    LINEAR.API_URL,
                    429,
                    "rate limited",
                    {"Retry-After": value},
                    None,
                )
                with (
                    patch.object(
                        LINEAR.urllib.request,
                        "urlopen",
                        side_effect=[limited, FakeResponse()],
                    ) as request,
                    patch.object(LINEAR.time, "sleep") as sleep,
                ):
                    self.assertEqual(
                        LINEAR.gql("key", "{ ok }"), {"ok": True}
                    )
                self.assertEqual(request.call_count, 2)
                sleep.assert_called_once_with(expected)


def replace_state(text, state):
    return LINEAR.replace_field(text, "State", state)


if __name__ == "__main__":
    unittest.main(verbosity=2)
