#!/usr/bin/env python3
"""Focused same-head CI rerun classification tests."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ci_rerun", ROOT / "scripts/ci-rerun.py")
assert SPEC and SPEC.loader
RERUN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RERUN)

REPO = "nysa/relay"
HEAD = "a" * 40


def check(name, bucket, job, workflow="ci", run=12):
    return {
        "bucket": bucket,
        "link": f"https://github.com/{REPO}/actions/runs/{run}/job/{job}",
        "name": name,
        "workflow": workflow,
    }


def aggregate_observation():
    required = [
        check("ci", "fail", 40),
        check("test-immutability", "pass", 10),
    ]
    checks = [
        *required,
        check("web-tests", "fail", 34),
        check("policy", "pass", 11),
        {
            "bucket": "pass", "link": "https://example.invalid/deploy/1",
            "name": "preview", "workflow": "",
        },
    ]
    run = {
        "conclusion": "failure", "databaseId": 12, "event": "pull_request",
        "headSha": HEAD, "status": "completed", "workflowName": "ci",
        "jobs": [
            {
                "conclusion": "success", "databaseId": 10,
                "name": "test-immutability", "status": "completed",
            },
            {
                "conclusion": "success", "databaseId": 11,
                "name": "policy", "status": "completed",
            },
            {
                "conclusion": "failure", "databaseId": 34,
                "name": "web-tests", "status": "completed",
            },
            {
                "conclusion": "failure", "databaseId": 40,
                "name": "ci", "status": "completed",
            },
        ],
    }
    return required, checks, run


def template_observation():
    required = [
        check("ci", "fail", 40),
        check("test-immutability", "pass", 10),
    ]
    run = {
        "conclusion": "failure", "databaseId": 12, "event": "pull_request",
        "headSha": HEAD, "status": "completed", "workflowName": "ci",
        "jobs": [
            {
                "conclusion": "success", "databaseId": 10,
                "name": "test-immutability", "status": "completed",
            },
            {
                "conclusion": "failure", "databaseId": 40, "name": "ci",
                "status": "completed",
                "steps": [
                    {
                        "conclusion": "success", "name": "Set up job",
                        "number": 1, "status": "completed",
                    },
                    {
                        "conclusion": "success", "name": "Run actions/checkout@v4",
                        "number": 2, "status": "completed",
                    },
                    {
                        "conclusion": "success", "name": "Run actions/setup-node@v4",
                        "number": 3, "status": "completed",
                    },
                    {
                        "conclusion": "failure", "name": "product tests",
                        "number": 4, "status": "completed",
                    },
                    {
                        "conclusion": "skipped", "name": "pin and product contract",
                        "number": 5, "status": "completed",
                    },
                    {
                        "conclusion": "skipped",
                        "name": "Post Run actions/setup-node@v4",
                        "number": 9, "status": "completed",
                    },
                    {
                        "conclusion": "success",
                        "name": "Post Run actions/checkout@v4",
                        "number": 10, "status": "completed",
                    },
                    {
                        "conclusion": "success", "name": "Complete job",
                        "number": 11, "status": "completed",
                    },
                ],
            },
        ],
    }
    return required, run


def current_template_observation():
    required, run = template_observation()
    run["jobs"][1]["steps"] = [
        {
            "conclusion": "success", "name": "Set up job",
            "number": 1, "status": "completed",
        },
        {
            "conclusion": "success", "name": "Run actions/checkout@v5",
            "number": 2, "status": "completed",
        },
        {
            "conclusion": "success", "name": "inspect qualification control",
            "number": 3, "status": "completed",
        },
        {
            "conclusion": "skipped", "name": "Run actions/checkout@v5",
            "number": 4, "status": "completed",
        },
        {
            "conclusion": "success", "name": "classify change",
            "number": 5, "status": "completed",
        },
        {
            "conclusion": "success", "name": "Run actions/setup-node@v5",
            "number": 6, "status": "completed",
        },
        {
            "conclusion": "success", "name": "npm ci",
            "number": 7, "status": "completed",
        },
        {
            "conclusion": "success", "name": "lint",
            "number": 8, "status": "completed",
        },
        {
            "conclusion": "success", "name": "typecheck",
            "number": 9, "status": "completed",
        },
        {
            "conclusion": "failure", "name": "tests",
            "number": 10, "status": "completed",
        },
        {
            "conclusion": "skipped", "name": "build",
            "number": 11, "status": "completed",
        },
        {
            "conclusion": "skipped",
            "name": "Post Run actions/setup-node@v5",
            "number": 15, "status": "completed",
        },
        {
            "conclusion": "success",
            "name": "Post Run actions/checkout@v5",
            "number": 16, "status": "completed",
        },
        {
            "conclusion": "success", "name": "Complete job",
            "number": 17, "status": "completed",
        },
    ]
    return required, run


class CiRerunTest(unittest.TestCase):
    def test_github_timeout_is_typed(self) -> None:
        with patch.object(
            RERUN.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(["gh", "pr", "view"], 120),
        ):
            with self.assertRaises(RERUN.ExternalUnavailable):
                RERUN.gh("pr", "view", "1")

    def test_direct_required_application_failure_is_retryable(self) -> None:
        required = [
            check("Security policy", "pass", 10),
            check("Application tests", "fail", 34),
        ]
        selected = RERUN.classify(required, required, REPO)
        self.assertEqual(selected, (12, 34, "Application tests", "ci", (34,)))

    def test_aggregate_required_failure_resolves_one_application_leaf(self) -> None:
        required, checks, run = aggregate_observation()
        selected = RERUN.classify(required, checks, REPO)
        self.assertEqual(selected, (12, 34, "web-tests", "ci", (34, 40)))
        RERUN.validate_run(run, HEAD, selected)

    def test_factory_template_aggregate_application_step_is_retryable(self) -> None:
        template = (ROOT / "ci/github-actions-ci.template.yml").read_text()
        for marker in (
            "actions/checkout@v5", "inspect qualification control",
            "classify change", "actions/setup-node@v5", "name: tests",
        ):
            self.assertIn(marker, template)
        for fixture in (template_observation, current_template_observation):
            with self.subTest(fixture=fixture.__name__):
                required, run = fixture()
                selected = RERUN.classify(required, required, REPO)
                self.assertEqual(selected, (12, 40, "ci", "ci", (40,)))
                RERUN.validate_run(run, HEAD, selected)

    def test_factory_template_aggregate_fails_closed(self) -> None:
        required, run = template_observation()
        selected = RERUN.classify(required, required, REPO)
        cases = {}

        control = copy.deepcopy(run)
        control["jobs"][1]["steps"][3]["name"] = "pin and product contract"
        cases["failed control"] = control

        multiple = copy.deepcopy(run)
        multiple["jobs"][1]["steps"][6]["conclusion"] = "failure"
        multiple["jobs"][1]["steps"][6]["name"] = "integration tests"
        cases["multiple application failures"] = multiple

        skipped_before = copy.deepcopy(run)
        skipped_before["jobs"][1]["steps"][0]["conclusion"] = "skipped"
        cases["skipped control before failure"] = skipped_before

        duplicate = copy.deepcopy(run)
        duplicate["jobs"][1]["steps"][2]["number"] = 2
        cases["duplicate step identity"] = duplicate

        extra = copy.deepcopy(run)
        extra["jobs"][1]["steps"].insert(5, {
            "conclusion": "success", "name": "tests and notify Slack",
            "number": 6, "status": "completed",
        })
        cases["unlisted side-effect step"] = extra

        for label, changed in cases.items():
            with self.subTest(label=label), self.assertRaises(RERUN.NotTransient):
                RERUN.validate_run(changed, HEAD, selected)

    def test_aggregate_resolution_fails_closed(self) -> None:
        required, checks, run = aggregate_observation()
        cases = {}

        multiple = copy.deepcopy(checks)
        multiple.append(check("api-tests", "fail", 35))
        cases["multiple leaf failures"] = (required, multiple)

        protected = copy.deepcopy(checks)
        protected[3]["bucket"] = "fail"
        cases["protected failure"] = (required, protected)

        pending = copy.deepcopy(required)
        pending[1]["bucket"] = "pending"
        cases["pending required check"] = (pending, checks)

        other_run = copy.deepcopy(checks)
        other_run[2] = check("web-tests", "fail", 34, run=13)
        cases["different workflow run"] = (required, other_run)

        external = copy.deepcopy(checks)
        external[2]["link"] = "https://example.invalid/status/34"
        cases["external failed status"] = (required, external)

        for label, (required_case, checks_case) in cases.items():
            with self.subTest(label), self.assertRaises(RERUN.NotTransient):
                RERUN.classify(required_case, checks_case, REPO)

        deploy = [
            check("deploy", "fail", 40, workflow="deploy"),
            check("test-immutability", "pass", 10, workflow="deploy"),
        ]
        with self.assertRaises(RERUN.NotTransient):
            RERUN.classify(deploy, deploy, REPO)

        selected = RERUN.classify(required, checks, REPO)
        for field, value in (
            ("databaseId", 13), ("headSha", "b" * 40),
            ("workflowName", "other"), ("status", "in_progress"),
        ):
            changed = copy.deepcopy(run)
            changed[field] = value
            with self.subTest(field), self.assertRaises(RERUN.NotTransient):
                RERUN.validate_run(changed, HEAD, selected)

        changed = copy.deepcopy(run)
        changed["jobs"].append({
            "conclusion": "failure", "databaseId": 35,
            "name": "api-tests", "status": "completed",
        })
        with self.assertRaises(RERUN.NotTransient):
            RERUN.validate_run(changed, HEAD, selected)


if __name__ == "__main__":
    unittest.main()
