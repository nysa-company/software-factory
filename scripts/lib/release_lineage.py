"""Ordered Contract 1.8 Factory-release lineage reduction."""

from __future__ import annotations

import re
from typing import Any, Callable


SHA = re.compile(r"^[0-9a-f]{40}$")
MIGRATION_SCHEMA = "nysa.software-factory.ticket-passport-migration/v2"


def release_edge(item: Any) -> bool:
    return (
        isinstance(item, dict)
        and item.get("schema") == MIGRATION_SCHEMA
        and SHA.fullmatch(item.get("from_factory_sha", "")) is not None
        and SHA.fullmatch(item.get("to_factory_sha", "")) is not None
    )


def successor_release_lineage(
    history: Any,
    migrations: Any,
    source: str,
    target: str,
    validator: Callable[[Any], bool] = release_edge,
) -> bool:
    if not isinstance(history, list) or not isinstance(migrations, list):
        return False
    releases = [
        item.get("factory_sha")
        for item in history
        if isinstance(item, dict)
        and set(item) == {"contract_version", "factory_sha"}
        and item.get("contract_version") == "1.8.0"
        and SHA.fullmatch(item.get("factory_sha", ""))
    ]
    if (
        len(releases) != len(history)
        or len(releases) != len(set(releases))
        or releases.count(source) != 1
        or releases.count(target) != 1
        or releases[-1] != target
    ):
        return False
    start = releases.index(source)
    expected = list(zip(releases[start:], releases[start + 1:]))
    if not expected:
        return source == target
    matches = 0
    for index, migration in enumerate(migrations):
        if (
            not validator(migration)
            or migration["from_factory_sha"] == migration["to_factory_sha"]
            or (migration["from_factory_sha"], migration["to_factory_sha"])
            != expected[0]
        ):
            continue
        suffix = migrations[index:]
        if all(validator(item) for item in suffix) and [
            (item["from_factory_sha"], item["to_factory_sha"])
            for item in suffix
            if item["from_factory_sha"] != item["to_factory_sha"]
        ] == expected:
            matches += 1
    return matches == 1
