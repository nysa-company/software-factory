#!/usr/bin/env python3
"""Prove one exact Factory, product, contract, Node, and npm tuple."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from certification_plan import (  # noqa: E402
    PREFLIGHT_SCHEMA, PlanError, TupleError, compare_tuple, diagnostic,
    expected_tuple, git_identity, observed_tuple, safe_plan, strict_tuple,
    validate_plan,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--factory-sha", required=True)
    parser.add_argument("--factory-tree", required=True)
    parser.add_argument("--product-root", required=True, type=Path)
    parser.add_argument("--contract-version", required=True)
    args = parser.parse_args()
    try:
        product = args.product_root.resolve(strict=True)
        plan, plan_digest = safe_plan(args.plan)
        phases = validate_plan(plan, product)
        product_sha, product_tree = git_identity(product)
        identity = {
            "contract_version": args.contract_version,
            "factory_sha": args.factory_sha,
            "factory_tree": args.factory_tree,
            "product_sha": product_sha,
            "product_tree": product_tree,
        }
        planned = expected_tuple(identity, plan)
        serialized = os.environ.get("FACTORY_CERTIFICATION_TUPLE")
        expected = strict_tuple(json.loads(serialized)) if serialized else planned
        compare_tuple(expected, planned)
        compare_tuple(expected, observed_tuple(identity))
        print(json.dumps({
            "optional_tests": sorted(
                name for name, phase in phases.items()
                if phase.get("optional") is True
            ),
            "phases": sorted(phases),
            "plan_sha256": plan_digest,
            "runtime_tuple": expected,
            "schema": PREFLIGHT_SCHEMA,
            "status": "pass",
        }, sort_keys=True, separators=(",", ":")))
        return 0
    except (
        FileNotFoundError, json.JSONDecodeError, OSError, PlanError, TupleError,
    ) as error:
        print(
            json.dumps(diagnostic(error), sort_keys=True, separators=(",", ":")),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
