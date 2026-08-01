#!/usr/bin/env python3
"""Resolve the Contract 1.8 ticket-budget stop from immutable run evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import re
import sys


QUALIFICATION_SCHEMA = "nysa.software-factory.qualification/v2"
SHA = re.compile(r"^[0-9a-f]{40}$")
TICKET = re.compile(r"^T-[0-9]+$")


def module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise ValueError(f"{name} is unavailable")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def resolve(
    kit: Path, product: Path, ticket: str, factory_sha: str | None = None
) -> str:
    if not TICKET.fullmatch(ticket):
        raise ValueError("invalid ticket")
    envelope = module("envelope_control", kit / "scripts/envelope-control.py")
    passport = module("ticket_passport", kit / "scripts/ticket-passport.py")
    values = envelope.parse_env_bytes(
        envelope.secure_read(product / "factory/ENVELOPE.env")
    )
    qualification_path = product / "factory/QUALIFICATION.json"
    qualification = None
    if qualification_path.exists() or qualification_path.is_symlink():
        qualification = json.loads(envelope.secure_read(qualification_path))
    successor = (
        isinstance(qualification, dict)
        and qualification.get("schema") == QUALIFICATION_SCHEMA
        and qualification.get("mode") == "successor"
    )
    if successor:
        tickets = qualification.get("tickets")
        expected = {
            "budget_usd", "capacity", "contract_version", "factory_sha",
            "generation", "mode", "per_run_budget_usd",
            "per_ticket_budget_usd", "schema", "source_factory_sha",
            "target_done", "tickets",
        }
        current = factory_sha or qualification.get("factory_sha")
        if (
            set(qualification) != expected
            or qualification.get("contract_version") != "1.8.0"
            or qualification.get("capacity") != 3
            or qualification.get("target_done") != 3
            or qualification.get("budget_usd") != "300.000000"
            or qualification.get("per_ticket_budget_usd") != "100.000000"
            or qualification.get("per_run_budget_usd") != "10.000000"
            or not SHA.fullmatch(current or "")
            or qualification.get("factory_sha") != current
            or not SHA.fullmatch(qualification.get("source_factory_sha", ""))
            or qualification["source_factory_sha"] == current
            or not isinstance(qualification.get("generation"), int)
            or isinstance(qualification.get("generation"), bool)
            or qualification["generation"] < 1
            or not isinstance(tickets, list)
            or len(tickets) != 3
            or any(not isinstance(item, str) or not TICKET.fullmatch(item) for item in tickets)
            or len(set(tickets)) != 3
            or ticket not in tickets
        ):
            raise ValueError("successor qualification budget is invalid")
        cap = 100_000_000
    else:
        _, changes = envelope.load_override_records(
            envelope.secure_directory(product / "factory"),
            ticket,
            "planner",
            datetime.now(timezone.utc).date().isoformat(),
            {"ticket"},
        )
        cap = int(Decimal(
            changes.get(
                "PER_TICKET_BUDGET_USD", values["PER_TICKET_BUDGET_USD"]
            )
        ) * 1_000_000)
    charges = passport.run_charges(product / "factory", ticket)
    spent = sum(
        item["charge_micro_usd"] for item in charges
        if not successor or item.get("factory_sha") == current
    )
    return (
        f"AWAIT_BUDGET ticket budget exhausted ({spent}/{cap} micro-USD)"
        if spent >= cap else "AVAILABLE"
    )


if __name__ == "__main__":
    try:
        print(resolve(
            Path(__file__).resolve().parent.parent,
            Path(sys.argv[1]).resolve(strict=True),
            sys.argv[2],
            sys.argv[3],
        ))
    except (IndexError, OSError, ValueError) as error:
        print(f"REFUSE {error}")
        raise SystemExit(1)
