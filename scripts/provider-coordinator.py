#!/usr/bin/env python3
"""Owner-local, fail-closed provider admission coordinator."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time


SCHEMA = "factory-provider-state/v2"
POLICY_SCHEMA = "factory-provider-concurrency-policy/v1"
OUTPUT_SCHEMA = "factory-provider-coordinator/v1"
APPLICATION_ID = 0x4E595343
ACTIVE_STATES = ("reserved", "GO", "submitted")
TERMINAL_RESULTS = frozenset(
    ("succeeded", "cancelled", "failed_pre_go", "failed", "capacity_denied")
)
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,199}$")
OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
MAX_MONEY = 10**15
MAX_WINDOW = 7 * 24 * 60 * 60
MAX_JSON = 1_000_000


class CoordinatorError(Exception):
    pass


def canonical(value):
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def validate_id(value, label, operation=False):
    pattern = OPERATION_ID if operation else SAFE_ID
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise CoordinatorError(f"{label} is invalid")
    return value


def validate_money(value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise CoordinatorError("reserve_micro_usd must be an integer")
    if value < 0 or value > MAX_MONEY:
        raise CoordinatorError("reserve_micro_usd is out of range")
    return value


def validate_day(value):
    if not isinstance(value, str):
        raise CoordinatorError("budget_day is invalid")
    try:
        parsed = datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise CoordinatorError("budget_day is invalid") from exc
    if parsed.isoformat() != value:
        raise CoordinatorError("budget_day is invalid")
    return value


def secure_directory(path):
    if not path.is_absolute():
        raise CoordinatorError("database path must be absolute")
    try:
        resolved = path.resolve(strict=True)
        info = path.lstat()
    except FileNotFoundError as exc:
        raise CoordinatorError(f"directory is missing: {path}") from exc
    if (
        resolved != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
    ):
        raise CoordinatorError("database directory is unsafe")
    return path


def secure_regular(path, label, maximum=None, owner_only=False):
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise CoordinatorError(f"{label} is missing") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or info.st_mode & 0o022
        or (owner_only and info.st_mode & 0o077)
        or (maximum is not None and info.st_size > maximum)
    ):
        raise CoordinatorError(f"{label} is unsafe")
    return info


def secure_read_json(path, label):
    if not path.is_absolute():
        raise CoordinatorError(f"{label} path must be absolute")
    expected = secure_regular(path, label, maximum=MAX_JSON)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        actual = os.fstat(descriptor)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise CoordinatorError(f"{label} changed while opening")
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = -1
            return json.load(handle)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def bounded_integer(value, label, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise CoordinatorError(f"{label} must be an integer from 1 through {maximum}")
    return value


def validate_limit(value, location):
    if not isinstance(value, dict) or set(value) != {
        "max_concurrent", "max_starts", "window_seconds"
    }:
        raise CoordinatorError(f"{location} must define exactly max_concurrent, max_starts, and window_seconds")
    return {
        "max_concurrent": bounded_integer(value["max_concurrent"], f"{location}.max_concurrent", 6),
        "max_starts": bounded_integer(value["max_starts"], f"{location}.max_starts", 1_000_000),
        "window_seconds": bounded_integer(value["window_seconds"], f"{location}.window_seconds", MAX_WINDOW),
    }


def validate_scope_map(value, location):
    if not isinstance(value, dict):
        raise CoordinatorError(f"{location} must be an object")
    result = {}
    for name, limits in value.items():
        validate_id(name, f"{location} key")
        result[name] = validate_limit(limits, f"{location}.{name}")
    return result


def load_policy(path):
    value = secure_read_json(path, "policy")
    required = {
        "schema", "coupled_max_concurrent", "global",
        "provider_families", "account_routes",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise CoordinatorError("policy has unsupported or missing fields")
    if value["schema"] != POLICY_SCHEMA:
        raise CoordinatorError("policy schema is unsupported")
    policy = {
        "schema": POLICY_SCHEMA,
        "coupled_max_concurrent": bounded_integer(
            value["coupled_max_concurrent"], "coupled_max_concurrent", 6
        ),
        "global": validate_limit(value["global"], "global"),
        "provider_families": validate_scope_map(
            value["provider_families"], "provider_families"
        ),
        "account_routes": validate_scope_map(
            value["account_routes"], "account_routes"
        ),
    }
    return policy, digest(policy)


def create_database(path):
    secure_directory(path.parent)
    if path.exists() or path.is_symlink():
        secure_regular(path, "database", owner_only=True)
        return
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        secure_regular(path, "database", owner_only=True)
        return
    os.close(descriptor)
    secure_regular(path, "database", owner_only=True)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS attempts (
  attempt_id TEXT PRIMARY KEY,
  provider_family TEXT NOT NULL,
  account_route TEXT NOT NULL,
  reserve_micro_usd INTEGER NOT NULL
    CHECK(reserve_micro_usd >= 0 AND reserve_micro_usd <= 1000000000000000),
  state TEXT NOT NULL
    CHECK(state IN ('prepared','reserved','GO','submitted','terminal')),
  version INTEGER NOT NULL CHECK(version >= 1),
  prepared_at INTEGER NOT NULL,
  admitted_at INTEGER,
  go_at INTEGER,
  submitted_at INTEGER,
  terminal_at INTEGER,
  terminal_result TEXT,
  policy_sha256 TEXT,
  updated_at INTEGER NOT NULL,
  CHECK(state != 'prepared' OR admitted_at IS NULL),
  CHECK(state IN ('prepared','terminal') OR admitted_at IS NOT NULL),
  CHECK((state != 'terminal') = (terminal_at IS NULL))
) STRICT;
CREATE INDEX IF NOT EXISTS attempts_active
  ON attempts(state, provider_family, account_route);
CREATE INDEX IF NOT EXISTS attempts_starts
  ON attempts(admitted_at, provider_family, account_route);
CREATE TABLE IF NOT EXISTS attempt_budgets (
  attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
  product_id TEXT NOT NULL,
  ticket_id TEXT NOT NULL,
  budget_day TEXT NOT NULL,
  product_daily_cap_micro_usd INTEGER NOT NULL,
  ticket_cap_micro_usd INTEGER NOT NULL,
  machine_daily_cap_micro_usd INTEGER NOT NULL,
  charge_micro_usd INTEGER,
  CHECK(length(budget_day) = 10),
  CHECK(product_daily_cap_micro_usd BETWEEN 0 AND 1000000000000000),
  CHECK(ticket_cap_micro_usd BETWEEN 0 AND 1000000000000000),
  CHECK(machine_daily_cap_micro_usd BETWEEN 0 AND 1000000000000000),
  CHECK(charge_micro_usd IS NULL OR charge_micro_usd BETWEEN 0 AND 1000000000000000)
) STRICT;
CREATE INDEX IF NOT EXISTS attempt_budgets_scope
  ON attempt_budgets(budget_day, product_id, ticket_id);
CREATE TABLE IF NOT EXISTS operations (
  operation_id TEXT PRIMARY KEY,
  command TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at INTEGER NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS legacy_intervals (
  interval_id TEXT PRIMARY KEY,
  product_id TEXT NOT NULL,
  started_at INTEGER NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS cancellation_requests (
  attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
  requested_at INTEGER NOT NULL,
  reason TEXT NOT NULL
) STRICT;
"""


def initialize(connection):
    connection.execute("BEGIN IMMEDIATE")
    try:
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        objects = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        if application_id not in (0, APPLICATION_ID) or user_version not in (0, 2):
            raise CoordinatorError("database identity or version is unsupported")
        if objects and (application_id != APPLICATION_ID or user_version != 2):
            raise CoordinatorError("non-empty database is not provider state-v2")
        for statement in SCHEMA_SQL.split(";"):
            if statement.strip():
                connection.execute(statement)
        connection.execute(
            "INSERT OR IGNORE INTO metadata(key,value) VALUES('schema',?)", (SCHEMA,)
        )
        stored = connection.execute(
            "SELECT value FROM metadata WHERE key='schema'"
        ).fetchone()
        if stored is None or stored[0] != SCHEMA:
            raise CoordinatorError("database schema marker is invalid")
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute("PRAGMA user_version=2")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


@contextmanager
def database(path):
    create_database(path)
    connection = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA trusted_schema=OFF")
        initialize(connection)
        secure_regular(path, "database", owner_only=True)
        yield connection
    finally:
        connection.close()
        secure_regular(path, "database", owner_only=True)


def row_result(row):
    result = dict(row)
    result["schema"] = OUTPUT_SCHEMA
    return result


def attempt(connection, attempt_id):
    row = connection.execute(
        """SELECT a.attempt_id,a.provider_family,a.account_route,
                  a.reserve_micro_usd,a.state,a.version,a.prepared_at,
                  a.admitted_at,a.go_at,a.submitted_at,a.terminal_at,
                  a.terminal_result,a.policy_sha256,a.updated_at,
                  b.product_id,b.ticket_id,b.budget_day,
                  b.product_daily_cap_micro_usd,b.ticket_cap_micro_usd,
                  b.machine_daily_cap_micro_usd,b.charge_micro_usd,
                  c.requested_at AS cancellation_requested_at,
                  c.reason AS cancellation_reason
           FROM attempts AS a
           JOIN attempt_budgets AS b ON b.attempt_id=a.attempt_id
           LEFT JOIN cancellation_requests AS c ON c.attempt_id=a.attempt_id
           WHERE a.attempt_id=?""",
        (attempt_id,),
    ).fetchone()
    if row is None:
        raise CoordinatorError("attempt does not exist")
    return row


def mutate(connection, operation_id, command, request, function):
    validate_id(operation_id, "operation_id", operation=True)
    request_sha256 = digest(request)
    connection.execute("BEGIN IMMEDIATE")
    try:
        prior = connection.execute(
            "SELECT command,request_sha256,result_json FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if prior is not None:
            if prior["command"] != command or prior["request_sha256"] != request_sha256:
                raise CoordinatorError("operation_id was already used for a different request")
            result = json.loads(prior["result_json"])
            connection.commit()
            return result
        result = function()
        result_json = canonical(result)
        connection.execute(
            "INSERT INTO operations VALUES(?,?,?,?,?)",
            (operation_id, command, request_sha256, result_json, int(time.time())),
        )
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise


def prepare_mutation(connection, values, now):
    existing = connection.execute(
        "SELECT * FROM attempts WHERE attempt_id=?", (values["attempt_id"],)
    ).fetchone()
    if existing is not None:
        expected = (
            existing["provider_family"], existing["account_route"],
            existing["reserve_micro_usd"],
        )
        actual = (
            values["provider_family"], values["account_route"],
            values["reserve_micro_usd"],
        )
        if expected != actual:
            raise CoordinatorError("attempt_id conflicts with an existing attempt")
        existing_budget = connection.execute(
            "SELECT * FROM attempt_budgets WHERE attempt_id=?",
            (values["attempt_id"],),
        ).fetchone()
        expected_budget = (
            values["product_id"], values["ticket_id"], values["budget_day"],
            values["product_daily_cap_micro_usd"], values["ticket_cap_micro_usd"],
            values["machine_daily_cap_micro_usd"],
        )
        actual_budget = (
            existing_budget["product_id"], existing_budget["ticket_id"],
            existing_budget["budget_day"],
            existing_budget["product_daily_cap_micro_usd"],
            existing_budget["ticket_cap_micro_usd"],
            existing_budget["machine_daily_cap_micro_usd"],
        )
        if expected_budget != actual_budget:
            raise CoordinatorError("attempt_id conflicts with an existing budget binding")
        return row_result(attempt(connection, values["attempt_id"]))
    connection.execute(
        """INSERT INTO attempts(
             attempt_id,provider_family,account_route,reserve_micro_usd,state,
             version,prepared_at,updated_at)
           VALUES(?,?,?,?,'prepared',1,?,?)""",
        (
            values["attempt_id"], values["provider_family"], values["account_route"],
            values["reserve_micro_usd"], now, now,
        ),
    )
    connection.execute(
        """INSERT INTO attempt_budgets(
             attempt_id,product_id,ticket_id,budget_day,
             product_daily_cap_micro_usd,ticket_cap_micro_usd,
             machine_daily_cap_micro_usd)
           VALUES(?,?,?,?,?,?,?)""",
        (
            values["attempt_id"], values["product_id"], values["ticket_id"],
            values["budget_day"], values["product_daily_cap_micro_usd"],
            values["ticket_cap_micro_usd"],
            values["machine_daily_cap_micro_usd"],
        ),
    )
    return row_result(attempt(connection, values["attempt_id"]))


def limit_checks(connection, policy, provider_family, account_route, now):
    scopes = [
        ("coupled", None, None, {"max_concurrent": policy["coupled_max_concurrent"]}),
        ("global", None, None, policy["global"]),
    ]
    if provider_family not in policy["provider_families"]:
        raise CoordinatorError("provider family has no concurrency policy")
    if account_route not in policy["account_routes"]:
        raise CoordinatorError("account route has no concurrency policy")
    scopes.extend([
        ("provider_family", "provider_family", provider_family,
         policy["provider_families"][provider_family]),
        ("account_route", "account_route", account_route,
         policy["account_routes"][account_route]),
    ])
    denials = []
    for name, column, selected, limits in scopes:
        where = "state IN ('reserved','GO','submitted')"
        parameters = []
        if column:
            where += f" AND {column}=?"
            parameters.append(selected)
        active = connection.execute(
            f"SELECT count(*) FROM attempts WHERE {where}", parameters
        ).fetchone()[0]
        if active >= limits["max_concurrent"]:
            denials.append({"limit": "max_concurrent", "scope": name})
        if "max_starts" in limits:
            start_where = "admitted_at IS NOT NULL AND admitted_at>?"
            start_parameters = [now - limits["window_seconds"]]
            if column:
                start_where += f" AND {column}=?"
                start_parameters.append(selected)
            starts = connection.execute(
                f"SELECT count(*) FROM attempts WHERE {start_where}", start_parameters
            ).fetchone()[0]
            if starts >= limits["max_starts"]:
                denials.append({"limit": "max_starts", "scope": name})
    return denials


def budget_checks(connection, row):
    scopes = [
        ("machine_day", "", (), row["machine_daily_cap_micro_usd"]),
        ("product_day", " AND b.product_id=?", (row["product_id"],),
         row["product_daily_cap_micro_usd"]),
        ("ticket", " AND b.product_id=? AND b.ticket_id=?",
         (row["product_id"], row["ticket_id"]), row["ticket_cap_micro_usd"]),
    ]
    denials = []
    for name, predicate, values, cap in scopes:
        spent = connection.execute(
            """SELECT coalesce(sum(
                   CASE WHEN a.state='terminal'
                        THEN b.charge_micro_usd ELSE a.reserve_micro_usd END
                 ),0)
               FROM attempts AS a
               JOIN attempt_budgets AS b ON b.attempt_id=a.attempt_id
               WHERE b.budget_day=?
                 AND (a.state IN ('reserved','GO','submitted')
                      OR (a.state='terminal' AND b.charge_micro_usd IS NOT NULL))"""
            + predicate,
            (row["budget_day"], *values),
        ).fetchone()[0]
        if spent + row["reserve_micro_usd"] > cap:
            denials.append({"limit": "budget_micro_usd", "scope": name})
    return denials


def admit_mutation(connection, attempt_id, expected_version, policy, policy_hash, now):
    row = attempt(connection, attempt_id)
    if row["state"] != "prepared":
        raise CoordinatorError("only a prepared attempt may be admitted")
    if row["version"] != expected_version:
        raise CoordinatorError("attempt version compare-and-swap failed")
    denials = []
    if connection.execute("SELECT 1 FROM legacy_intervals LIMIT 1").fetchone():
        denials.append({"limit": "legacy_barrier", "scope": "machine"})
    denials.extend(limit_checks(
        connection, policy, row["provider_family"], row["account_route"], now
    ))
    if connection.execute(
        """SELECT count(*) FROM attempts AS a
           JOIN attempt_budgets AS b ON b.attempt_id=a.attempt_id
           WHERE a.state IN ('reserved','GO','submitted')
             AND b.product_id=? AND b.ticket_id=?""",
        (row["product_id"], row["ticket_id"]),
    ).fetchone()[0]:
        denials.append({"limit": "max_concurrent", "scope": "ticket"})
    denials.extend(budget_checks(connection, row))
    if denials:
        return {
            "admitted": False,
            "attempt": row_result(row),
            "denials": denials,
            "schema": OUTPUT_SCHEMA,
        }
    changed = connection.execute(
        """UPDATE attempts
           SET state='reserved',version=version+1,admitted_at=?,
               policy_sha256=?,updated_at=?
           WHERE attempt_id=? AND state='prepared' AND version=?""",
        (now, policy_hash, now, attempt_id, expected_version),
    ).rowcount
    if changed != 1:
        raise CoordinatorError("attempt changed during admission")
    return {
        "admitted": True,
        "attempt": row_result(attempt(connection, attempt_id)),
        "denials": [],
        "schema": OUTPUT_SCHEMA,
    }


def transition_mutation(
    connection, attempt_id, expected_version, target, now, result=None, charge=None
):
    row = attempt(connection, attempt_id)
    allowed = {
        "GO": ("reserved",),
        "submitted": ("GO",),
        "terminal": ("prepared", "reserved", "GO", "submitted"),
    }[target]
    if row["version"] != expected_version:
        raise CoordinatorError("attempt version compare-and-swap failed")
    if row["state"] not in allowed:
        raise CoordinatorError(f"attempt cannot transition from {row['state']} to {target}")
    if target == "terminal":
        charge = row["reserve_micro_usd"] if charge is None else validate_money(charge)
    timestamps = {
        "GO": ("go_at",),
        "submitted": ("submitted_at",),
        "terminal": ("terminal_at", "terminal_result"),
    }
    columns = timestamps[target]
    assignments = ["state=?", "version=version+1", "updated_at=?"]
    parameters = [target, now]
    for column in columns:
        assignments.append(f"{column}=?")
        parameters.append(now if column.endswith("_at") else result)
    parameters.extend((attempt_id, expected_version))
    changed = connection.execute(
        f"UPDATE attempts SET {','.join(assignments)} WHERE attempt_id=? AND version=?",
        parameters,
    ).rowcount
    if changed != 1:
        raise CoordinatorError("attempt changed during transition")
    if target == "terminal":
        changed = connection.execute(
            """UPDATE attempt_budgets SET charge_micro_usd=?
               WHERE attempt_id=? AND charge_micro_usd IS NULL""",
            (charge, attempt_id),
        ).rowcount
        if changed != 1:
            raise CoordinatorError("attempt charge changed during transition")
    return row_result(attempt(connection, attempt_id))


def common_attempt_values(args):
    return {
        "attempt_id": validate_id(args.attempt_id, "attempt_id"),
        "provider_family": validate_id(args.provider_family, "provider_family"),
        "account_route": validate_id(args.account_route, "account_route"),
        "reserve_micro_usd": validate_money(args.reserve_micro_usd),
        "product_id": validate_id(args.product_id, "product_id"),
        "ticket_id": validate_id(args.ticket_id, "ticket_id"),
        "budget_day": validate_day(args.budget_day),
        "product_daily_cap_micro_usd": validate_money(
            args.product_daily_cap_micro_usd
        ),
        "ticket_cap_micro_usd": validate_money(args.ticket_cap_micro_usd),
        "machine_daily_cap_micro_usd": validate_money(
            args.machine_daily_cap_micro_usd
        ),
    }


def now_value(args):
    if args.now is None:
        return int(time.time())
    if args.now < 0:
        raise CoordinatorError("--now must be a non-negative Unix timestamp")
    return args.now


def prepare_command(connection, args):
    values = common_attempt_values(args)
    now = now_value(args)
    request = dict(values, now=args.now)
    return mutate(
        connection, args.operation_id, "prepare", request,
        lambda: prepare_mutation(connection, values, now),
    )


def admit_command(connection, args):
    attempt_id = validate_id(args.attempt_id, "attempt_id")
    policy, policy_hash = load_policy(Path(args.policy))
    now = now_value(args)
    request = {
        "attempt_id": attempt_id, "expected_version": args.expected_version,
        "now": args.now, "policy_sha256": policy_hash,
    }
    return mutate(
        connection, args.operation_id, "admit", request,
        lambda: admit_mutation(
            connection, attempt_id, args.expected_version, policy, policy_hash, now
        ),
    )


def reserve_command(connection, args):
    values = common_attempt_values(args)
    policy, policy_hash = load_policy(Path(args.policy))
    if (args.expected_policy_sha256 is not None and
            args.expected_policy_sha256 != policy_hash):
        raise CoordinatorError("provider policy does not match the activated digest")
    now = now_value(args)
    request = dict(
        values, now=args.now, policy_sha256=policy_hash,
        expected_policy_sha256=args.expected_policy_sha256,
    )

    def reserve():
        prepared = prepare_mutation(connection, values, now)
        if prepared["state"] != "prepared" or prepared["version"] != 1:
            raise CoordinatorError("reserve replay requires the original prepared attempt")
        return admit_mutation(
            connection, values["attempt_id"], 1, policy, policy_hash, now
        )

    return mutate(connection, args.operation_id, "reserve", request, reserve)


def transition_command(connection, args, target):
    attempt_id = validate_id(args.attempt_id, "attempt_id")
    now = now_value(args)
    result = getattr(args, "result", None)
    if result is not None and result not in TERMINAL_RESULTS:
        raise CoordinatorError("terminal result is unsupported")
    charge = getattr(args, "charge_micro_usd", None)
    if charge is not None:
        charge = validate_money(charge)
    request = {
        "attempt_id": attempt_id, "expected_version": args.expected_version,
        "now": args.now, "target": target, "result": result, "charge": charge,
    }
    return mutate(
        connection, args.operation_id, target.lower(), request,
        lambda: transition_mutation(
            connection, attempt_id, args.expected_version, target, now, result,
            charge,
        ),
    )


def request_cancel_command(connection, args):
    attempt_id = validate_id(args.attempt_id, "attempt_id")
    reason = validate_id(args.reason, "reason")
    now = now_value(args)
    request = {
        "attempt_id": attempt_id,
        "expected_version": args.expected_version,
        "now": args.now,
        "reason": reason,
    }

    def persist():
        row = attempt(connection, attempt_id)
        if row["version"] != args.expected_version or row["state"] != "submitted":
            raise CoordinatorError(
                "cancellation request requires the expected submitted attempt"
            )
        existing = connection.execute(
            "SELECT requested_at,reason FROM cancellation_requests WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO cancellation_requests VALUES(?,?,?)",
                (attempt_id, now, reason),
            )
        elif existing["reason"] != reason:
            raise CoordinatorError("cancellation request conflicts")
        return row_result(attempt(connection, attempt_id))

    return mutate(
        connection, args.operation_id, "request-cancel", request, persist
    )


def status_command(connection, args):
    if args.attempt_id:
        rows = [attempt(connection, validate_id(args.attempt_id, "attempt_id"))]
    else:
        rows = [
            attempt(connection, row[0])
            for row in connection.execute(
                "SELECT attempt_id FROM attempts ORDER BY attempt_id"
            ).fetchall()
        ]
    counts = dict(
        connection.execute(
            "SELECT state,count(*) FROM attempts GROUP BY state ORDER BY state"
        ).fetchall()
    )
    return {
        "active_reserve_micro_usd": connection.execute(
            "SELECT coalesce(sum(reserve_micro_usd),0) FROM attempts "
            "WHERE state IN ('reserved','GO','submitted')"
        ).fetchone()[0],
        "attempts": [dict(row) for row in rows],
        "counts": counts,
        "legacy_intervals": [
            dict(row)
            for row in connection.execute(
                "SELECT interval_id,product_id,started_at "
                "FROM legacy_intervals ORDER BY interval_id"
            ).fetchall()
        ],
        "schema": OUTPUT_SCHEMA,
    }


def legacy_enter_command(connection, args):
    interval_id = validate_id(args.interval_id, "interval_id")
    product_id = validate_id(args.product_id, "product_id")
    now = now_value(args)
    request = {
        "interval_id": interval_id,
        "product_id": product_id,
        "now": args.now,
    }

    def enter():
        existing = connection.execute(
            "SELECT * FROM legacy_intervals WHERE interval_id=?", (interval_id,)
        ).fetchone()
        if existing is not None:
            if existing["product_id"] != product_id:
                raise CoordinatorError("legacy interval identity conflicts")
            return {
                "entered": True,
                "interval": dict(existing),
                "schema": OUTPUT_SCHEMA,
            }
        active = connection.execute(
            "SELECT count(*) FROM attempts WHERE state IN ('reserved','GO','submitted')"
        ).fetchone()[0]
        if active:
            return {
                "denials": [{"limit": "isolated_barrier", "scope": "machine"}],
                "entered": False,
                "schema": OUTPUT_SCHEMA,
            }
        connection.execute(
            "INSERT INTO legacy_intervals VALUES(?,?,?)",
            (interval_id, product_id, now),
        )
        return {
            "entered": True,
            "interval": {
                "interval_id": interval_id,
                "product_id": product_id,
                "started_at": now,
            },
            "schema": OUTPUT_SCHEMA,
        }

    return mutate(
        connection, args.operation_id, "legacy-enter", request, enter
    )


def legacy_exit_command(connection, args):
    interval_id = validate_id(args.interval_id, "interval_id")
    request = {"interval_id": interval_id, "now": args.now}
    now_value(args)

    def leave():
        changed = connection.execute(
            "DELETE FROM legacy_intervals WHERE interval_id=?", (interval_id,)
        ).rowcount
        return {
            "exited": changed == 1,
            "interval_id": interval_id,
            "schema": OUTPUT_SCHEMA,
        }

    return mutate(
        connection, args.operation_id, "legacy-exit", request, leave
    )


def reconcile_command(connection, args):
    observations = secure_read_json(Path(args.input), "reconciliation input")
    if (
        not isinstance(observations, dict)
        or set(observations) != {"schema", "observations"}
        or observations["schema"] != "factory-provider-observations/v1"
        or not isinstance(observations["observations"], list)
    ):
        raise CoordinatorError("reconciliation input schema is invalid")
    normalized = []
    seen = set()
    for item in observations["observations"]:
        if not isinstance(item, dict) or set(item) != {
            "attempt_id", "expected_version", "outcome"
        }:
            raise CoordinatorError("reconciliation observation is invalid")
        attempt_id = validate_id(item["attempt_id"], "attempt_id")
        if attempt_id in seen:
            raise CoordinatorError("reconciliation repeats an attempt")
        seen.add(attempt_id)
        outcome = item["outcome"]
        if outcome not in {"succeeded", "cancelled", "failed", "unknown"}:
            raise CoordinatorError("reconciliation outcome is invalid")
        if isinstance(item["expected_version"], bool) or not isinstance(item["expected_version"], int):
            raise CoordinatorError("reconciliation expected_version is invalid")
        normalized.append(item)
    now = now_value(args)
    request = {"input_sha256": digest(observations), "now": args.now}

    def reconcile():
        results = []
        for item in normalized:
            row = attempt(connection, item["attempt_id"])
            if item["outcome"] == "unknown" or (
                item["outcome"] == "failed"
                and row["state"] in {"GO", "submitted"}
            ):
                results.append({
                    "attempt_id": item["attempt_id"],
                    "action": "retained",
                    "state": row["state"],
                })
                continue
            terminal_result = (
                "failed_pre_go"
                if item["outcome"] == "failed"
                else item["outcome"]
            )
            transitioned = transition_mutation(
                connection, item["attempt_id"], item["expected_version"],
                "terminal", now, terminal_result,
            )
            results.append({
                "attempt_id": item["attempt_id"],
                "action": "terminalized",
                "state": transitioned["state"],
            })
        return {"results": results, "schema": OUTPUT_SCHEMA}

    return mutate(connection, args.operation_id, "reconcile", request, reconcile)


def parser():
    result = argparse.ArgumentParser()
    result.add_argument("--db", required=True)
    commands = result.add_subparsers(dest="command", required=True)

    def mutation(name):
        command = commands.add_parser(name)
        command.add_argument("--operation-id", required=True)
        command.add_argument("--now", type=int)
        return command

    prepare = mutation("prepare")
    reserve = mutation("reserve")
    for command in (prepare, reserve):
        command.add_argument("--attempt-id", required=True)
        command.add_argument("--provider-family", required=True)
        command.add_argument("--account-route", required=True)
        command.add_argument("--reserve-micro-usd", required=True, type=int)
        command.add_argument("--product-id", required=True)
        command.add_argument("--ticket-id", required=True)
        command.add_argument("--budget-day", required=True)
        command.add_argument(
            "--product-daily-cap-micro-usd", required=True, type=int
        )
        command.add_argument("--ticket-cap-micro-usd", required=True, type=int)
        command.add_argument(
            "--machine-daily-cap-micro-usd", required=True, type=int
        )
    prepare.set_defaults(handler=prepare_command)
    reserve.add_argument("--policy", required=True)
    reserve.add_argument("--expected-policy-sha256")
    reserve.set_defaults(handler=reserve_command)

    admit = mutation("admit")
    admit.add_argument("--attempt-id", required=True)
    admit.add_argument("--expected-version", required=True, type=int)
    admit.add_argument("--policy", required=True)
    admit.set_defaults(handler=admit_command)

    for name, target in (("mark-go", "GO"), ("mark-submitted", "submitted")):
        command = mutation(name)
        command.add_argument("--attempt-id", required=True)
        command.add_argument("--expected-version", required=True, type=int)
        command.set_defaults(
            handler=lambda connection, args, selected=target:
            transition_command(connection, args, selected)
        )

    terminal = mutation("terminalize")
    terminal.add_argument("--attempt-id", required=True)
    terminal.add_argument("--expected-version", required=True, type=int)
    terminal.add_argument("--result", required=True)
    terminal.add_argument("--charge-micro-usd", required=True, type=int)
    terminal.set_defaults(
        handler=lambda connection, args:
        transition_command(connection, args, "terminal")
    )

    cancellation = mutation("request-cancel")
    cancellation.add_argument("--attempt-id", required=True)
    cancellation.add_argument("--expected-version", required=True, type=int)
    cancellation.add_argument("--reason", required=True)
    cancellation.set_defaults(handler=request_cancel_command)

    status = commands.add_parser("status")
    status.add_argument("--attempt-id")
    status.set_defaults(handler=status_command)

    legacy_enter = mutation("legacy-enter")
    legacy_enter.add_argument("--interval-id", required=True)
    legacy_enter.add_argument("--product-id", required=True)
    legacy_enter.set_defaults(handler=legacy_enter_command)

    legacy_exit = mutation("legacy-exit")
    legacy_exit.add_argument("--interval-id", required=True)
    legacy_exit.set_defaults(handler=legacy_exit_command)

    reconcile = mutation("reconcile")
    reconcile.add_argument("--input", required=True)
    reconcile.set_defaults(handler=reconcile_command)
    return result


def main():
    try:
        args = parser().parse_args()
        path = Path(args.db)
        with database(path) as connection:
            output = args.handler(connection, args)
        print(canonical(output))
    except (
        CoordinatorError, OSError, sqlite3.Error, UnicodeError,
        json.JSONDecodeError, ValueError,
    ) as exc:
        print(canonical({"error": str(exc), "schema": OUTPUT_SCHEMA, "status": "error"}))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
