#!/usr/bin/env python3
"""Focused same-head CI rerun classification tests."""

from __future__ import annotations

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


class CiRerunTest(unittest.TestCase):
    def test_github_timeout_is_typed(self) -> None:
        with patch.object(
            RERUN.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(["gh", "pr", "view"], 120),
        ):
            with self.assertRaises(RERUN.ExternalUnavailable):
                RERUN.gh("pr", "view", "1")

    def test_only_one_exact_application_test_job_is_retryable(self) -> None:
        run, job, name = RERUN.classify([
            {"bucket": "pass", "link": "", "name": "Security policy"},
            {
                "bucket": "fail",
                "link": "https://github.com/nysa/relay/actions/runs/12/job/34",
                "name": "Application tests",
            },
        ], "nysa/relay")
        self.assertEqual((run, job, name), (12, 34, "Application tests"))
        with self.assertRaises(RERUN.RerunError):
            RERUN.classify([{
                "bucket": "fail",
                "link": "https://github.com/nysa/relay/actions/runs/12/job/34",
                "name": "Application tests",
            }], "nysa/relay")
        for protected in ("Security tests", "Factory control", "Config policy"):
            with self.assertRaises(RERUN.RerunError):
                RERUN.classify([{
                    "bucket": "fail",
                    "link": "https://github.com/nysa/relay/actions/runs/12/job/34",
                    "name": protected,
                }], "nysa/relay")


if __name__ == "__main__":
    unittest.main()
