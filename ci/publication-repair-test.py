#!/usr/bin/env python3
"""Focused typed publication-repair sequence test."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "publication_repair", ROOT / "scripts/publication-repair.py"
)
assert SPEC and SPEC.loader
REPAIR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPAIR)


class PublicationRepairTest(unittest.TestCase):
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
