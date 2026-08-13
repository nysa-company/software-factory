"""Hydrate immutable Git objects named by committed terminal migrations."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
from typing import Any


SHA = re.compile(r"[0-9a-f]{40}\Z")


class HistoricalObjectError(ValueError):
    pass


def _repository(product: Path) -> str:
    values = re.findall(
        r"^(?:export\s+)?GH_REPO\s*=\s*['\"]?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)['\"]?\s*$",
        (product / "factory/PROJECT.env").read_text(encoding="utf-8"),
        re.M,
    )
    if len(values) != 1:
        raise HistoricalObjectError("historical product repository is ambiguous")
    return values[0]


def commit_present(product: Path, sha: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(product), "cat-file", "-e", f"{sha}^{{commit}}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=120,
    ).returncode == 0


def hydrate(product: Path) -> int:
    migrations = product / "factory/migrations"
    if not migrations.is_dir():
        return 0
    supported = {
        "nysa.software-factory.legacy-closeout/v1": ("pr",),
        "nysa.software-factory.terminal-backfill/v1": (
            "implementation_pr", "closeout_pr",
        ),
        "nysa.software-factory.protected-merge-reconciliation/v1": (
            "original_pr", "adoption_pr",
        ),
    }
    repository = _repository(product)
    requirements: dict[tuple[int, str], dict[str, Any]] = {}
    for path in sorted(migrations.glob("**/*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HistoricalObjectError(
                f"historical object record is malformed: {path.relative_to(product)}"
            ) from error
        keys = supported.get(value.get("schema")) if isinstance(value, dict) else None
        if not keys:
            continue
        relative = str(path.relative_to(product))
        if value.get("repository") != repository:
            raise HistoricalObjectError(
                f"historical object repository mismatch: {relative}"
            )
        for key in keys:
            record = value.get(key)
            if record is None:
                continue
            if (
                not isinstance(record, dict)
                or isinstance(record.get("number"), bool)
                or not isinstance(record.get("number"), int)
                or record["number"] <= 0
                or not SHA.fullmatch(record.get("head", ""))
            ):
                raise HistoricalObjectError(
                    f"historical PR record is malformed: {relative} {key}"
                )
            identity = record["number"], record["head"]
            item = requirements.setdefault(identity, {"commits": set(), "paths": set()})
            item["commits"].add(record["head"])
            item["paths"].add(relative)
            if (
                value.get("schema")
                == "nysa.software-factory.protected-merge-reconciliation/v1"
                and key == "original_pr"
            ):
                evidence = value.get("evidence_head", "")
                if not SHA.fullmatch(evidence):
                    raise HistoricalObjectError(
                        f"historical evidence head is malformed: {relative}"
                    )
                item["commits"].add(evidence)

    for (number, head), item in sorted(requirements.items()):
        missing = sorted(
            sha for sha in item["commits"] if not commit_present(product, sha)
        )
        if missing:
            reference = f"refs/pull/{number}/head"
            observed = subprocess.run(
                ["git", "-C", str(product), "ls-remote", "--refs", "origin", reference],
                text=True, capture_output=True, check=False, timeout=120,
            )
            fields = observed.stdout.split()
            relative = sorted(item["paths"])[0]
            if observed.returncode or fields != [head, reference]:
                raise HistoricalObjectError(
                    f"historical PR head unavailable: {relative} PR #{number} expected {head}"
                )
            fetched = subprocess.run(
                [
                    "git", "-C", str(product), "fetch", "--quiet", "--no-tags",
                    "--no-write-fetch-head", "origin", reference,
                ],
                text=True, capture_output=True, check=False, timeout=120,
            )
            if fetched.returncode:
                raise HistoricalObjectError(
                    f"historical PR head fetch failed: {relative} PR #{number} expected {head}"
                )
        absent = sorted(
            sha for sha in item["commits"] if not commit_present(product, sha)
        )
        if absent:
            raise HistoricalObjectError(
                f"historical commit object missing: {sorted(item['paths'])[0]} "
                f"PR #{number} expected {absent[0]}"
            )
        for sha in item["commits"]:
            if sha != head and subprocess.run(
                ["git", "-C", str(product), "merge-base", "--is-ancestor", sha, head],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=120,
            ).returncode:
                raise HistoricalObjectError(
                    f"historical commit is not in PR: {sorted(item['paths'])[0]} "
                    f"PR #{number} expected {sha}"
                )
    return len(requirements)
