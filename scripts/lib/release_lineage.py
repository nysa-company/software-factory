"""Ordered Contract 1.8 Factory-release lineage reduction."""

from __future__ import annotations

import re
from typing import Any, Callable


SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
MIGRATION_SCHEMA = "nysa.software-factory.ticket-passport-migration/v2"


def release_edge(item: Any) -> bool:
    return (
        isinstance(item, dict)
        and item.get("schema") == MIGRATION_SCHEMA
        and SHA.fullmatch(item.get("from_factory_sha", "")) is not None
        and SHA.fullmatch(item.get("to_factory_sha", "")) is not None
    )


def valid_v2_migration(item: Any) -> bool:
    sha_fields = {
        "from_factory_sha", "from_head_sha", "from_protected_base_sha",
        "to_factory_sha", "to_head_sha", "to_protected_base_sha",
    }
    digest_fields = {
        "from_passport_file_sha256", "from_passport_sha256",
        "from_route_plan_sha256", "to_route_plan_sha256",
    }
    required = sha_fields | digest_fields | {"schema"}
    optional = {
        "rewrite_authorization_sha256",
        "lineage_authorization_blob",
        "lineage_authorization_commit",
        "lineage_authorization_path",
        "lineage_authorization_sha256",
    }
    authorization = {
        name for name in optional if name.startswith("lineage_authorization_")
    }
    present = set(item) & authorization if isinstance(item, dict) else set()
    return (
        isinstance(item, dict)
        and required.issubset(item)
        and set(item).issubset(required | optional)
        and item.get("schema") == MIGRATION_SCHEMA
        and all(
            isinstance(item.get(name), str) and SHA.fullmatch(item[name])
            for name in sha_fields
        )
        and all(
            isinstance(item.get(name), str) and DIGEST.fullmatch(item[name])
            for name in digest_fields
        )
        and (
            "rewrite_authorization_sha256" not in item
            or (
                isinstance(item["rewrite_authorization_sha256"], str)
                and DIGEST.fullmatch(item["rewrite_authorization_sha256"])
            )
        )
        and (not present or present == authorization)
        and (
            not present
            or (
                isinstance(item["lineage_authorization_blob"], str)
                and SHA.fullmatch(item["lineage_authorization_blob"])
                and isinstance(item["lineage_authorization_commit"], str)
                and SHA.fullmatch(item["lineage_authorization_commit"])
                and isinstance(item["lineage_authorization_path"], str)
                and re.fullmatch(
                    r"factory/migrations/ticket-passport-lineage/"
                    r"[0-9a-f]{40}/T-[0-9]+\.json",
                    item["lineage_authorization_path"],
                )
                and isinstance(item["lineage_authorization_sha256"], str)
                and DIGEST.fullmatch(item["lineage_authorization_sha256"])
            )
        )
    )


def passport_head_lineage(passport: Any, source_head: str) -> bool:
    """Match one authenticated v2 suffix from source to the current passport."""
    if (
        not isinstance(passport, dict)
        or not isinstance(source_head, str)
        or not SHA.fullmatch(source_head)
        or not isinstance(passport.get("head_sha"), str)
        or not SHA.fullmatch(passport["head_sha"])
    ):
        return False
    if source_head == passport["head_sha"]:
        return True
    migrations = passport.get("migration_history")
    if (
        not isinstance(migrations, list)
        or not isinstance(passport.get("factory_sha"), str)
        or not SHA.fullmatch(passport["factory_sha"])
        or not isinstance(passport.get("protected_base_sha"), str)
        or not SHA.fullmatch(passport["protected_base_sha"])
        or not isinstance(passport.get("route_plan_sha256"), str)
        or not DIGEST.fullmatch(passport["route_plan_sha256"])
        or not isinstance(passport.get("parent_file_sha256"), str)
        or not DIGEST.fullmatch(passport["parent_file_sha256"])
        or not isinstance(passport.get("parent_digest"), str)
        or not DIGEST.fullmatch(passport["parent_digest"])
    ):
        return False
    matches = 0
    for index, edge in enumerate(migrations):
        previous = migrations[index - 1] if index else None
        anchored = index == 0 or (
            valid_v2_migration(previous)
            and previous["from_head_sha"] != source_head
            and previous["to_head_sha"] == source_head
        )
        if (
            not valid_v2_migration(edge)
            or edge["from_head_sha"] != source_head
            or not anchored
        ):
            continue
        suffix = migrations[index:]
        if (
            all(valid_v2_migration(item) for item in suffix)
            and all(
                prior["to_factory_sha"] == following["from_factory_sha"]
                and prior["to_head_sha"] == following["from_head_sha"]
                and prior["to_protected_base_sha"]
                == following["from_protected_base_sha"]
                and prior["to_route_plan_sha256"]
                == following["from_route_plan_sha256"]
                for prior, following in zip(suffix, suffix[1:])
            )
            and suffix[-1]["to_factory_sha"] == passport["factory_sha"]
            and suffix[-1]["to_head_sha"] == passport["head_sha"]
            and suffix[-1]["to_protected_base_sha"]
            == passport["protected_base_sha"]
            and suffix[-1]["to_route_plan_sha256"]
            == passport["route_plan_sha256"]
            and suffix[-1]["from_passport_file_sha256"]
            == passport["parent_file_sha256"]
            and suffix[-1]["from_passport_sha256"]
            == passport["parent_digest"]
        ):
            matches += 1
    return matches == 1


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
