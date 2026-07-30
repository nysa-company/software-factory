#!/usr/bin/env python3
"""Resolve the Contract 1.8 ticket-budget stop from immutable run evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import importlib.util
from pathlib import Path
import re
import sys


def module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise ValueError(f"{name} is unavailable")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def resolve(kit: Path, product: Path, ticket: str) -> str:
    if not re.fullmatch(r"T-[0-9]+", ticket):
        raise ValueError("invalid ticket")
    envelope = module("envelope_control", kit / "scripts/envelope-control.py")
    passport = module("ticket_passport", kit / "scripts/ticket-passport.py")
    values = envelope.parse_env_bytes(
        envelope.secure_read(product / "factory/ENVELOPE.env")
    )
    _, changes = envelope.load_override_records(
        envelope.secure_directory(product / "factory"),
        ticket,
        "planner",
        datetime.now(timezone.utc).date().isoformat(),
        {"ticket"},
    )
    cap = int(Decimal(
        changes.get("PER_TICKET_BUDGET_USD", values["PER_TICKET_BUDGET_USD"])
    ) * 1_000_000)
    charges = passport.run_charges(product / "factory", ticket)
    spent = sum(item["charge_micro_usd"] for item in charges)
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
        ))
    except (IndexError, OSError, ValueError) as error:
        print(f"REFUSE {error}")
        raise SystemExit(1)
