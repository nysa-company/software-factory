"""Shared strict validation for the Contract 1.8 qualification manifest."""

from __future__ import annotations

import re
from typing import Any


SCHEMA = "nysa.software-factory.qualification/v2"
SHA = re.compile(r"[0-9a-f]{40}\Z")
TICKET = re.compile(r"T-[0-9]+\Z")


class ManifestError(ValueError):
    pass


def validate(
    value: Any, factory_sha: str, expected_capacity: int | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError("qualification manifest must be an object")
    successor = value.get("mode") == "successor"
    expected_keys = {
        "budget_usd", "capacity", "contract_version", "factory_sha",
        "generation", "per_run_budget_usd", "per_ticket_budget_usd",
        "schema", "target_done", "tickets",
    } | ({"mode", "source_factory_sha"} if successor else set())
    tickets = value.get("tickets")
    target = value.get("target_done")
    capacity = value.get("capacity")
    if (
        set(value) != expected_keys
        or value.get("schema") != SCHEMA
        or value.get("contract_version") != "1.8.0"
        or not SHA.fullmatch(factory_sha)
        or value.get("factory_sha") != factory_sha
        or capacity not in (3, 4)
        or expected_capacity is not None and capacity != expected_capacity
        or target not in (3, 4)
        or target > capacity
        or not isinstance(value.get("generation"), int)
        or isinstance(value.get("generation"), bool)
        or value["generation"] < 1
        or not isinstance(tickets, list)
        or len(tickets) != target
        or len(tickets) != len(set(tickets))
        or any(not isinstance(ticket, str) or not TICKET.fullmatch(ticket)
               for ticket in tickets)
        or successor and (
            capacity != 3
            or target != 3
            or value.get("budget_usd") != "300.000000"
            or value.get("per_ticket_budget_usd") != "100.000000"
            or value.get("per_run_budget_usd") != "10.000000"
            or not SHA.fullmatch(value.get("source_factory_sha", ""))
            or value["source_factory_sha"] == factory_sha
        )
        or not successor and (
            value.get("budget_usd") != "100.000000"
            or value.get("per_ticket_budget_usd") != "25.000000"
            or value.get("per_run_budget_usd") != "2.000000"
        )
    ):
        raise ManifestError("Contract 1.8 qualification manifest is invalid")
    return value
