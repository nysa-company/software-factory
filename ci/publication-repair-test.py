#!/usr/bin/env python3
"""Focused typed publication-repair sequence test."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "publication_repair", ROOT / "scripts/publication-repair.py"
)
assert SPEC and SPEC.loader
REPAIR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPAIR)


class PublicationRepairTest(unittest.TestCase):
    def test_remote_timeout_is_typed_but_local_timeout_is_not(self):
        timeout = subprocess.TimeoutExpired(["gh", "pr", "view"], 120)
        with patch.object(REPAIR.subprocess, "run", side_effect=timeout):
            with self.assertRaises(REPAIR.ExternalUnavailable):
                REPAIR.command("gh", "pr", "view", "1")
            with self.assertRaises(subprocess.TimeoutExpired):
                REPAIR.command("git", "status")

    def test_exact_push_survives_a_lost_response(self):
        branch = "ticket/T-1"
        head, before = "b" * 40, "a" * 40
        results = [
            subprocess.CompletedProcess(
                [], 128, "", "remote response was lost",
            ),
            subprocess.CompletedProcess(
                [], 0, f"{head}\trefs/heads/{branch}\n", "",
            ),
            subprocess.CompletedProcess([], 0, before + "\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        with patch.object(REPAIR.subprocess, "run", side_effect=results):
            REPAIR.push_head(Path("/cell"), "origin", branch, head, before)

    def test_only_named_repair_then_fresh_review_and_narration_run(self):
        record = {"repair_owner": "builder", "verdict_baseline": 1}
        old = "Reviewer round 1: APPROVE\n"
        self.assertEqual(REPAIR.decide(record, old, []), "FIX builder")
        self.assertEqual(REPAIR.decide(record, old, ["builder"]), "RUN reviewer")
        approved = old + "Reviewer round 2: APPROVE\n"
        self.assertEqual(
            REPAIR.decide(record, approved, ["builder", "reviewer"]),
            "RUN narrator",
        )
        self.assertTrue(
            REPAIR.decide(
                record, approved, ["builder", "reviewer", "narrator"]
            ).startswith("AWAIT-OPERATOR")
        )
        rejected = old + "Reviewer round 2: REQUEST CHANGES — fixture\n"
        self.assertEqual(
            REPAIR.decide(record, rejected, ["builder", "reviewer"]),
            "INACTIVE",
        )


if __name__ == "__main__":
    unittest.main()
