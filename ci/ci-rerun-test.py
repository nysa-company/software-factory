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
