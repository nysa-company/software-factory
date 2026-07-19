#!/usr/bin/env python3
"""Exact decimal arithmetic for factory budget checks."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path


DECIMAL_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")


def decimal(value: str) -> Decimal:
    if len(value) > 1000 or not DECIMAL_RE.fullmatch(value):
        raise ValueError(f"invalid decimal: {value}")
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"invalid decimal: {value}") from error


def display(value: Decimal, minimum_places: int = 0) -> str:
    rendered = format(value, "f")
    whole, separator, fraction = rendered.partition(".")
    if not separator:
        fraction = ""
    fraction = fraction.rstrip("0")
    if len(fraction) < minimum_places:
        fraction += "0" * (minimum_places - len(fraction))
    return f"{whole}.{fraction}" if fraction else whole


def sum_csv(args: argparse.Namespace) -> int:
    total = Decimal(0)
    with Path(args.csv).open(newline="", encoding="utf-8") as handle:
        rows = csv.reader(handle)
        next(rows, None)
        with localcontext() as context:
            context.prec = 1000
            for row in rows:
                if len(row) <= max(args.date_column, args.amount_column):
                    raise ValueError("accounting row has too few columns")
                if args.date is not None and row[args.date_column] != args.date:
                    continue
                if args.filter_column is not None:
                    if len(row) <= args.filter_column:
                        raise ValueError("accounting row has too few columns")
                    if row[args.filter_column] != args.filter_value:
                        continue
                total += decimal(row[args.amount_column])
    print(display(total, minimum_places=4))
    return 0


def reserve(args: argparse.Namespace) -> int:
    budget = decimal(args.budget)
    spent = decimal(args.spent)
    cap = decimal(args.cap)
    with localcontext() as context:
        context.prec = 1000
        remaining = cap - spent
    if Decimal(0) < remaining < budget:
        print(display(remaining, minimum_places=4))
    else:
        print(args.budget)
    return 0


def exceeds(args: argparse.Namespace) -> int:
    with localcontext() as context:
        context.prec = 1000
        projected = decimal(args.spent) + decimal(args.reserve)
    return 0 if projected > decimal(args.cap) else 1


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)

    sum_parser = commands.add_parser("sum-csv")
    sum_parser.add_argument("--csv", required=True)
    sum_parser.add_argument("--date")
    sum_parser.add_argument("--date-column", type=int, required=True)
    sum_parser.add_argument("--amount-column", type=int, required=True)
    sum_parser.add_argument("--filter-column", type=int)
    sum_parser.add_argument("--filter-value")
    sum_parser.set_defaults(function=sum_csv)

    reserve_parser = commands.add_parser("reserve")
    reserve_parser.add_argument("--budget", required=True)
    reserve_parser.add_argument("--spent", required=True)
    reserve_parser.add_argument("--cap", required=True)
    reserve_parser.set_defaults(function=reserve)

    exceeds_parser = commands.add_parser("exceeds")
    exceeds_parser.add_argument("--spent", required=True)
    exceeds_parser.add_argument("--reserve", required=True)
    exceeds_parser.add_argument("--cap", required=True)
    exceeds_parser.set_defaults(function=exceeds)
    return root


def main() -> int:
    args = parser().parse_args()
    if (args.command == "sum-csv" and
            ((args.filter_column is None) != (args.filter_value is None))):
        raise ValueError("filter column and value must be provided together")
    return args.function(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"money: {error}", file=sys.stderr)
        raise SystemExit(2)
