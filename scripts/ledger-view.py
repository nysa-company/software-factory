#!/usr/bin/env python3
"""Build or project the effective factory ledger from durable rows and manifests."""

import argparse
from contextlib import contextmanager
import csv
import hashlib
import json
import math
import os
from datetime import datetime
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time


FIELDS = (
    "date", "time", "ticket", "role", "adapter", "prompt_version", "turns",
    "cost_usd", "exit_status", "run_id", "provider_family", "model_id",
    "selection_reason", "cost_basis", "adapter_version",
)
HEADERS = {
    tuple(FIELDS[:9]),
    tuple(FIELDS[:11]),
    FIELDS,
}
TERMINAL_STATES = {"completed", "launch_void", "abandoned_conservative"}
MAX_COST_USD = 1_000_000
MAX_TURNS = 1_000_000


def fail(message):
    raise ValueError(message)


def validate_ledger_row(row, number, path):
    cost = row["cost_usd"]
    if (not re.fullmatch(r"[0-9]{1,7}(?:\.[0-9]{1,18})?", cost) or
            float(cost) > MAX_COST_USD):
        fail(f"invalid durable ledger cost at row {number} in {path}")
    turns = row["turns"]
    if (not re.fullmatch(r"[0-9]{1,7}", turns) or int(turns) > MAX_TURNS):
        fail(f"invalid durable ledger turns at row {number} in {path}")
    run_id = row["run_id"]
    if run_id and not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", run_id):
        fail(f"invalid durable ledger run_id at row {number} in {path}")
    status = row["exit_status"]
    if not (re.fullmatch(r"[0-9]{1,3}", status) and int(status) <= 255):
        expected = f"reserved-{run_id}" if run_id else ""
        if not run_id or status != expected:
            fail(f"invalid durable ledger exit status at row {number} in {path}")


def read_csv(path):
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) not in HEADERS:
            fail(f"unsupported ledger schema: {','.join(reader.fieldnames or ())}")
        rows = []
        for number, raw in enumerate(reader, 2):
            if None in raw:
                fail(f"malformed ledger row {number} in {path}")
            row = {field: (raw.get(field) or "") for field in FIELDS}
            validate_ledger_row(row, number, path)
            rows.append(row)
        return rows


def read_meta(path):
    values = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line or "=" not in line:
            fail(f"malformed manifest line {number} in {path}")
        key, value = line.split("=", 1)
        if key in values:
            fail(f"duplicate manifest key {key} in {path}")
        values[key] = value
    return values


def number(value, label):
    if not re.fullmatch(r"[0-9]{1,7}(?:\.[0-9]{1,18})?", value or ""):
        fail(f"invalid {label}: {value!r}")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        fail(f"invalid {label}: {value!r}")
    if not math.isfinite(parsed) or parsed < 0 or parsed > MAX_COST_USD:
        fail(f"invalid {label}: {value!r}")
    return value


def manifest_row(path):
    values = read_meta(path)
    if values.get("accounting_schema") != "1":
        return None
    required = (
        "run_id", "ticket", "role", "adapter", "provider_family",
        "selection_reason", "adapter_version", "reserved_usd", "go_issued",
        "started_at", "prompt_version", "accounting_state",
    )
    if any(not values.get(key) for key in required):
        fail(f"incomplete accounting manifest: {path}")
    run_id = values["run_id"]
    if path.stem != run_id or not re.fullmatch(r"[A-Za-z0-9._-]+", run_id):
        fail(f"manifest run identity mismatch: {path}")
    if not re.fullmatch(r"T-[0-9]+", values["ticket"]):
        fail(f"invalid manifest ticket: {path}")
    if values["go_issued"] not in ("0", "1"):
        fail(f"invalid GO marker: {path}")
    state = values["accounting_state"]
    if state not in {"reserved", *TERMINAL_STATES}:
        fail(f"invalid accounting state {state!r}: {path}")
    reserved = number(values["reserved_usd"], "reserved cost")
    timestamp = values["started_at"]
    try:
        stamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        fail(f"invalid accounting timestamp: {path}")
    if state in TERMINAL_STATES:
        try:
            datetime.fromisoformat(values.get("terminal_at", "").replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            fail(f"invalid terminal accounting timestamp: {path}")

    turns = values.get("turns", "0") or "0"
    if not re.fullmatch(r"[0-9]{1,7}", turns) or int(turns) > MAX_TURNS:
        fail(f"invalid turn count: {path}")
    if state == "reserved":
        cost, status, basis = reserved, f"reserved-{run_id}", "conservative_reservation"
    else:
        status = values.get("exit_status", "")
        if not re.fullmatch(r"[0-9]{1,3}", status) or int(status) > 255:
            fail(f"invalid terminal exit status: {path}")
        if state == "launch_void":
            if values["go_issued"] != "0":
                fail(f"launch_void manifest issued GO: {path}")
            cost, turns, basis = "0", "0", "launch_void"
        elif state == "abandoned_conservative":
            if values["go_issued"] != "1":
                fail(f"post-GO manifest lacks GO marker: {path}")
            cost, basis = reserved, "conservative_reservation"
        else:
            if values["go_issued"] != "1":
                fail(f"completed manifest lacks GO marker: {path}")
            cost = number(values.get("effective_cost", ""), "effective cost")
            basis = values.get("cost_basis", "")
            if not basis:
                fail(f"completed manifest lacks cost basis: {path}")

    return {
        "date": stamp.strftime("%Y-%m-%d"),
        "time": stamp.strftime("%H:%M:%S"),
        "ticket": values["ticket"],
        "role": values["role"],
        "adapter": values["adapter"],
        "prompt_version": "reserved" if state == "reserved" else values["prompt_version"],
        "turns": turns,
        "cost_usd": cost,
        "exit_status": status,
        "run_id": run_id,
        "provider_family": values["provider_family"],
        "model_id": values["model_id"],
        "selection_reason": values["selection_reason"],
        "cost_basis": basis,
        "adapter_version": values["adapter_version"],
    }


def is_reservation(row):
    return row["prompt_version"] == "reserved" or row["exit_status"].startswith("reserved-")


def merge_rows(durable, manifests):
    rows = []
    indexes = {}
    sources = {}

    def add(row, source):
        run_id = row["run_id"]
        if not run_id:
            rows.append(row)
            return
        if run_id not in indexes:
            indexes[run_id] = len(rows)
            sources[run_id] = source
            rows.append(row)
            return
        index = indexes[run_id]
        current = rows[index]
        if current == row:
            return
        current_source = sources[run_id]
        if (current_source == "durable" and source == "manifest" and
                is_reservation(current) and not is_reservation(row)):
            rows[index] = row
            sources[run_id] = source
            return
        fail(f"conflicting {source} records for run_id {run_id}")

    for row in durable:
        add(row, "durable")
    for row in manifests:
        if row:
            add(row, "manifest")
    return rows


def csv_bytes(rows):
    import io
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode()


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def atomic_write_in_directory(directory, name, content):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(directory, flags)
    temp = f".{name}.{os.getpid()}.{secrets.token_hex(8)}"
    descriptor = -1
    try:
        descriptor = os.open(
            temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temp, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def effective_rows(factory_root, durable=None, runtime=None, runs=None):
    factory = factory_root / "factory"
    durable = durable or factory / "ledger.csv"
    runs = runs or factory / "runs"
    manifests = []
    if runs.is_dir():
        paths = sorted(runs.glob("*.meta"))
        for path in paths:
            if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
                fail(f"run manifest must be a regular non-symlink file: {path.name}")
        manifests = [manifest_row(path) for path in paths]
    # runtime is deliberately output-only. The effective view is reduced from
    # committed durable history plus authoritative run manifests every time.
    return merge_rows(read_csv(durable), manifests)


def git(path, *arguments, check=True):
    result = subprocess.run(
        ["git", "-C", str(path), *arguments], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if check and result.returncode:
        fail(result.stderr.strip() or f"git {' '.join(arguments)} failed")
    return result


def common_dir(path):
    raw = git(path, "rev-parse", "--git-common-dir").stdout.strip()
    value = Path(raw) if os.path.isabs(raw) else path / raw
    return value.resolve()


def git_dir(path):
    raw = git(path, "rev-parse", "--git-dir").stdout.strip()
    value = Path(raw) if os.path.isabs(raw) else path / raw
    return value.resolve()


def validate_projection(source, workdir, ticket):
    source, workdir = source.resolve(), workdir.resolve()
    if not re.fullmatch(r"T-[0-9]+", ticket):
        fail("invalid ticket identifier")
    if workdir == source or not workdir.is_dir() or workdir.is_symlink():
        fail("projection requires a distinct linked worktree")
    top = Path(git(workdir, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if top != workdir or common_dir(source) != common_dir(workdir):
        fail("projection worktree does not belong to the product repository")
    if git_dir(workdir) == common_dir(workdir):
        fail("projection requires a linked worktree")
    branch = git(workdir, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    expected = f"chore/{ticket.lower().replace('-', '')}-closeout"
    if branch != expected:
        fail(f"projection branch must be {expected}")
    if git(workdir, "status", "--porcelain").stdout:
        fail("projection worktree must be clean")
    git(workdir, "rev-parse", "--verify", "origin/main")
    if git(workdir, "merge-base", "--is-ancestor", "origin/main", "HEAD", check=False).returncode:
        fail("projection branch is not based on current origin/main")


def validate_projection_target(workdir):
    factory = workdir / "factory"
    ledger = factory / "ledger.csv"
    if factory.is_symlink() or not factory.is_dir():
        fail("projection factory directory must be a real directory")
    try:
        factory.resolve().relative_to(workdir.resolve())
    except ValueError:
        fail("projection factory directory escapes the worktree")
    if ledger.is_symlink() or (ledger.exists() and not ledger.is_file()):
        fail("projection ledger must be a regular non-symlink file")


@contextmanager
def projection_locks(factory_root):
    acquired = []
    try:
        for lock in (factory_root / "factory" / ".launch.lock", factory_root / "factory" / ".ledger.lock"):
            for _ in range(50):
                try:
                    lock.mkdir()
                    acquired.append(lock)
                    break
                except FileExistsError:
                    time.sleep(0.2)
            else:
                fail(f"projection lock is stuck: {lock.name}")
        yield
    finally:
        for lock in reversed(acquired):
            try:
                lock.rmdir()
            except FileNotFoundError:
                pass


def unsettled_manifest(factory_root):
    runs = factory_root / "factory" / "runs"
    if not runs.is_dir():
        return None
    for path in sorted(runs.glob("*.meta")):
        values = read_meta(path)
        if values.get("accounting_schema") == "1":
            settled = values.get("accounting_state") in TERMINAL_STATES
        else:
            settled = values.get("phase") in {"completed", "abandoned"}
        if not settled:
            return path, values.get("ticket", "unknown")
    return None


def unmatched_legacy_terminal_manifest(factory_root, rows):
    accounted = {
        (row["ticket"], row["run_id"])
        for row in rows if row["run_id"] and not is_reservation(row)
    }
    runs = factory_root / "factory" / "runs"
    if not runs.is_dir():
        return None
    for path in sorted(runs.glob("*.meta")):
        values = read_meta(path)
        if (values.get("accounting_schema") != "1" and
                values.get("phase") in {"completed", "abandoned"} and
                (values.get("ticket"), values.get("run_id")) not in accounted):
            return path, values.get("ticket", "unknown")
    return None


def ensure_projectable(factory_root, rows=None):
    unsettled = unsettled_manifest(factory_root)
    if unsettled:
        path, ticket = unsettled
        fail(f"product has a live or ambiguous manifest for {ticket}: {path.name}")
    if rows is None:
        return
    reservation = next((row for row in rows if is_reservation(row)), None)
    if reservation:
        fail(f"product has a live or ambiguous run for {reservation['ticket']}")
    unmatched = unmatched_legacy_terminal_manifest(factory_root, rows)
    if unmatched:
        path, ticket = unmatched
        fail(f"product has an unaccounted legacy manifest for {ticket}: {path.name}")


def paths(args):
    root = Path(args.factory_root).resolve()
    durable = Path(args.durable_ledger).resolve() if args.durable_ledger else None
    runtime = Path(args.runtime_ledger).resolve() if args.runtime_ledger else None
    runs = Path(args.runs_dir).resolve() if args.runs_dir else None
    return root, durable, runtime, runs


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("refresh", "print"):
        child = subparsers.add_parser(command)
        child.add_argument("--factory-root", required=True)
        child.add_argument("--durable-ledger")
        child.add_argument("--runtime-ledger")
        child.add_argument("--runs-dir")
    project = subparsers.add_parser("project")
    project.add_argument("--factory-root", required=True)
    project.add_argument("--workdir", required=True)
    project.add_argument("--ticket", required=True)
    args = parser.parse_args()

    if args.command in ("refresh", "print"):
        root, durable, runtime, runs = paths(args)
        rows = effective_rows(root, durable, runtime, runs)
        content = csv_bytes(rows)
        if args.command == "print":
            sys.stdout.buffer.write(content)
        else:
            target = runtime or root / "factory" / "runtime-ledger.csv"
            atomic_write(target, content)
            print(target)
        return

    root = Path(args.factory_root).resolve()
    workdir = Path(args.workdir).resolve()
    validate_projection(root, workdir, args.ticket)
    validate_projection_target(workdir)
    with projection_locks(root):
        ensure_projectable(root)
        rows = effective_rows(root)
        ensure_projectable(root, rows)
        content = csv_bytes(rows)
        # Recheck immediately before the atomic write while launch and
        # reservation remain blocked by their existing locks.
        ensure_projectable(root, rows)
        validate_projection_target(workdir)
        atomic_write_in_directory(workdir / "factory", "ledger.csv", content)
    ticket_cost = sum(float(row["cost_usd"] or 0) for row in rows if row["ticket"] == args.ticket)
    print(json.dumps({
        "schema": "nysa.software-factory.ledger-projection/v1",
        "schema_version": 1,
        "status": "ok",
        "ticket": args.ticket,
        "row_count": len(rows),
        "ticket_cost_usd": round(ticket_cost, 6),
        "sha256": hashlib.sha256(content).hexdigest(),
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print(f"ledger-view: {error}", file=sys.stderr)
        raise SystemExit(1)
