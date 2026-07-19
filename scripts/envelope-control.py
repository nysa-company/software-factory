#!/usr/bin/env python3
"""Inspect and change operating envelopes without executing product code.

All mutating commands use a canonical preview hash and compare-and-swap against
the exact input files. Temporary overrides are immutable records; consuming a
``next-attempt`` record creates a receipt instead of rewriting the record or
any prior accounting.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys


ROLES = ("planner", "narrator", "builder", "spec-linter", "test-author", "reviewer")
BASE_KEYS = (
    "PER_RUN_BUDGET_USD",
    "PER_TICKET_BUDGET_USD",
    "PER_RUN_MAX_TURNS",
    "PER_RUN_TIMEOUT_MIN",
    "DAILY_CAP_USD",
)
ROLE_SUFFIXES = (
    "PER_RUN_BUDGET_USD",
    "PER_RUN_MAX_TURNS",
    "PER_RUN_TIMEOUT_MIN",
)
ROLE_KEYS = tuple(
    f"{role.upper().replace('-', '_')}_{suffix}"
    for role in ROLES
    for suffix in ROLE_SUFFIXES
)
ALL_KEYS = BASE_KEYS + ROLE_KEYS
MONEY_KEYS = {key for key in ALL_KEYS if key.endswith("_USD")}
INTEGER_KEYS = {key for key in ALL_KEYS if key.endswith("_TURNS") or key.endswith("_MIN")}
MAX_MONEY = 1_000_000
MAX_TURNS = 1_000
MAX_TIMEOUT = 1_440
LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=([A-Za-z0-9._:/+@%~-]*)$")
MONEY = re.compile(r"^[0-9]{1,7}(?:\.[0-9]{1,6})?$")
INTEGER = re.compile(r"^[0-9]{1,4}$")
HASH = re.compile(r"^[0-9a-f]{64}$")
TICKET = re.compile(r"^T-[0-9]+$")
RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
UTC_DATE = re.compile(r"^20[0-9]{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])$")


class ControlError(Exception):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def fail(message):
    print(json.dumps({"error": message, "status": "error"}, sort_keys=True, separators=(",", ":")))
    raise SystemExit(2)


def secure_directory(path, create=False):
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ControlError(f"directory is missing: {path}") from exc
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise ControlError(f"directory must be physical: {path}")
    if info.st_uid != os.geteuid():
        raise ControlError(f"directory is not owned by the current operator: {path}")
    if info.st_mode & 0o022:
        raise ControlError(f"directory is group/world writable: {path}")
    return path


def secure_file(path):
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ControlError(f"file is missing: {path}") from exc
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_nlink != 1:
        raise ControlError(f"file must be a regular single-link file: {path}")
    if info.st_uid != os.geteuid():
        raise ControlError(f"file is not owned by the current operator: {path}")
    if info.st_mode & 0o022:
        raise ControlError(f"file is group/world writable: {path}")
    return info


def secure_read(path, maximum=None):
    """Read one operator-owned inode without following a last-component link."""
    expected = secure_file(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        actual = os.fstat(descriptor)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise ControlError(f"file changed while being opened: {path}")
        if maximum is not None and actual.st_size > maximum:
            raise ControlError(f"file is oversized: {path}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def factory_paths(root):
    if root.is_symlink():
        raise ControlError("factory root may not be a symlink")
    root = root.resolve(strict=True)
    secure_directory(root)
    factory = secure_directory(root / "factory")
    env_path = factory / "ENVELOPE.env"
    md_path = factory / "ENVELOPE.md"
    secure_file(env_path)
    secure_file(md_path)
    return root, factory, env_path, md_path


def parse_env_bytes(raw, require_base=True):
    values = {}
    for raw_line in raw.decode("utf-8").splitlines():
        if not raw_line or raw_line.startswith("#"):
            continue
        line = raw_line
        if line.startswith("export "):
            line = line[7:]
        match = LINE.fullmatch(line)
        if not match:
            raise ControlError("envelope must contain plain KEY=value lines")
        key, value = match.groups()
        if key not in ALL_KEYS:
            raise ControlError(f"envelope contains unsupported key {key}")
        if key in values:
            raise ControlError(f"envelope repeats {key}")
        values[key] = value
    missing = [key for key in BASE_KEYS if key not in values]
    if require_base and missing:
        raise ControlError(f"envelope is missing {missing[0]}")
    validate_values(values)
    return values


def positive_money(key, value):
    if not MONEY.fullmatch(value):
        raise ControlError(f"{key} must be a bounded positive decimal")
    number = float(value)
    if not 0 < number <= MAX_MONEY:
        raise ControlError(f"{key} must be a bounded positive decimal")
    return number


def positive_integer(key, value):
    if not INTEGER.fullmatch(value):
        raise ControlError(f"{key} must be a bounded positive integer")
    number = int(value)
    maximum = MAX_TIMEOUT if key.endswith("_MIN") else MAX_TURNS
    if not 0 < number <= maximum:
        raise ControlError(f"{key} must be a bounded positive integer")
    return number


def validate_values(values):
    for key, value in values.items():
        if key in MONEY_KEYS:
            positive_money(key, value)
        elif key in INTEGER_KEYS:
            positive_integer(key, value)
        else:
            raise ControlError(f"unsupported envelope value {key}")
    if all(key in values for key in BASE_KEYS):
        ticket = positive_money("PER_TICKET_BUDGET_USD", values["PER_TICKET_BUDGET_USD"])
        daily = positive_money("DAILY_CAP_USD", values["DAILY_CAP_USD"])
        for role in (None,) + ROLES:
            prefix = "" if role is None else role.upper().replace("-", "_") + "_"
            key = prefix + "PER_RUN_BUDGET_USD"
            budget = positive_money(key, values.get(key, values["PER_RUN_BUDGET_USD"]))
            if budget > ticket or budget > daily:
                raise ControlError(f"{key} exceeds the ticket or daily cap")


def effective_role(values, role):
    if role not in ROLES:
        raise ControlError(f"unsupported role: {role}")
    prefix = role.upper().replace("-", "_") + "_"
    return {
        "PER_RUN_BUDGET_USD": values.get(prefix + "PER_RUN_BUDGET_USD", values["PER_RUN_BUDGET_USD"]),
        "PER_TICKET_BUDGET_USD": values["PER_TICKET_BUDGET_USD"],
        "PER_RUN_MAX_TURNS": values.get(prefix + "PER_RUN_MAX_TURNS", values["PER_RUN_MAX_TURNS"]),
        "PER_RUN_TIMEOUT_MIN": values.get(prefix + "PER_RUN_TIMEOUT_MIN", values["PER_RUN_TIMEOUT_MIN"]),
        "DAILY_CAP_USD": values["DAILY_CAP_USD"],
    }


def render_env(original, values):
    comments = [
        line for line in original.decode("utf-8").splitlines()
        if not line or line.startswith("#")
    ]
    output = comments
    if output and output[-1]:
        output.append("")
    output.extend(f"{key}={values[key]}" for key in BASE_KEYS)
    role_values = [key for key in ROLE_KEYS if key in values]
    if role_values:
        output.extend(("", "# Optional role limits; omitted values inherit the defaults above."))
        output.extend(f"{key}={values[key]}" for key in role_values)
    return ("\n".join(output).rstrip() + "\n").encode()


MD_LABELS = {
    "PER_RUN_BUDGET_USD": ("Per-run budget (USD)", "$"),
    "PER_TICKET_BUDGET_USD": ("Per-ticket budget (USD)", "$"),
    "PER_RUN_MAX_TURNS": ("Per-run max turns", ""),
    "PER_RUN_TIMEOUT_MIN": ("Per-run wall-clock cap", ""),
    "DAILY_CAP_USD": ("Daily factory cap (USD)", "$"),
}


def validate_markdown(raw, values):
    text = raw.decode("utf-8")
    for key, (label, prefix) in MD_LABELS.items():
        suffix = " min" if key == "PER_RUN_TIMEOUT_MIN" else ""
        pattern = re.compile(
            rf"^\|\s*{re.escape(label)}\s*\|\s*([^|]*?)\s*\|", re.MULTILINE
        )
        matches = pattern.findall(text)
        if len(matches) != 1:
            raise ControlError(f"ENVELOPE.md has no unique {label} row")
        if matches[0].strip() != f"{prefix}{values[key]}{suffix}":
            raise ControlError(f"ENVELOPE.md and ENVELOPE.env disagree on {key}")
    expected = {}
    for role in ROLES:
        prefix = role.upper().replace("-", "_") + "_"
        if any(prefix + suffix in values for suffix in ROLE_SUFFIXES):
            selected = effective_role(values, role)
            expected[role] = (
                f"${selected['PER_RUN_BUDGET_USD']}",
                selected["PER_RUN_MAX_TURNS"],
                f"{selected['PER_RUN_TIMEOUT_MIN']} min",
            )
    role_rows = re.findall(
        r"^\|\s*(planner|narrator|builder|spec-linter|test-author|reviewer)\s*"
        r"\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|$",
        text,
        re.MULTILINE,
    )
    actual = {role: (budget.strip(), turns.strip(), timeout.strip())
              for role, budget, turns, timeout in role_rows}
    if actual != expected:
        raise ControlError("ENVELOPE.md role limits disagree with ENVELOPE.env")


def render_markdown(original, values):
    text = original.decode("utf-8")
    for key, (label, prefix) in MD_LABELS.items():
        suffix = " min" if key == "PER_RUN_TIMEOUT_MIN" else ""
        pattern = re.compile(rf"^(\|\s*{re.escape(label)}\s*\|\s*)[^|]*(\|.*)$", re.MULTILINE)
        replacement = rf"\g<1>{prefix}{values[key]}{suffix} \g<2>"
        text, count = pattern.subn(replacement, text, count=1)
        if count != 1:
            raise ControlError(f"ENVELOPE.md has no unique {label} row")
    start = "<!-- BEGIN ROLE ENVELOPE LIMITS -->"
    end = "<!-- END ROLE ENVELOPE LIMITS -->"
    rows = []
    for role in ROLES:
        prefix = role.upper().replace("-", "_") + "_"
        present = [prefix + suffix for suffix in ROLE_SUFFIXES if prefix + suffix in values]
        if present:
            effective = effective_role(values, role)
            rows.append(
                f"| {role} | ${effective['PER_RUN_BUDGET_USD']} | "
                f"{effective['PER_RUN_MAX_TURNS']} | {effective['PER_RUN_TIMEOUT_MIN']} min |"
            )
    block = ""
    if rows:
        block = (
            f"{start}\n\n### Optional per-role attempt limits\n\n"
            "| Role | Budget | Max turns | Timeout |\n"
            "|---|---:|---:|---:|\n" + "\n".join(rows) + f"\n\n{end}"
        )
    region = re.compile(rf"\n?{re.escape(start)}.*?{re.escape(end)}\n?", re.DOTALL)
    text = region.sub("\n", text)
    if block:
        marker = "\n## Retries and escalation"
        if marker not in text:
            raise ControlError("ENVELOPE.md is missing the retries section")
        text = text.replace(marker, "\n\n" + block + marker, 1)
    return (text.rstrip() + "\n").encode()


def read_state(root):
    root, factory, env_path, md_path = factory_paths(root)
    env_raw = secure_read(env_path, 1_000_000)
    md_raw = secure_read(md_path, 2_000_000)
    values = parse_env_bytes(env_raw)
    validate_markdown(md_raw, values)
    return root, factory, env_path, md_path, env_raw, md_raw, values


def read_runtime_state(root):
    if root.is_symlink():
        raise ControlError("factory root may not be a symlink")
    root = root.resolve(strict=True)
    secure_directory(root)
    factory = secure_directory(root / "factory")
    env_path = factory / "ENVELOPE.env"
    secure_file(env_path)
    env_raw = secure_read(env_path, 1_000_000)
    return root, factory, env_path, env_raw, parse_env_bytes(env_raw)


def changes_from_args(items):
    changes = {}
    for item in items:
        if "=" not in item:
            raise ControlError("--set requires KEY=value")
        key, value = item.split("=", 1)
        if key not in ALL_KEYS:
            raise ControlError(f"unsupported envelope key {key}")
        if key in changes:
            raise ControlError(f"duplicate change for {key}")
        changes[key] = value
    if not changes:
        raise ControlError("at least one --set is required")
    return changes


def envelope_preview(root, changes):
    state = read_state(root)
    _, _, _, _, env_raw, md_raw, old = state
    new = dict(old)
    new.update(changes)
    validate_values(new)
    env_new = render_env(env_raw, new)
    md_new = render_markdown(md_raw, new)
    body = {
        "base": {"env_sha256": digest(env_raw), "markdown_sha256": digest(md_raw)},
        "changes": changes,
        "result": {
            "env_sha256": digest(env_new),
            "markdown_sha256": digest(md_new),
            "values": new,
        },
        "schema": "factory-envelope-preview/v1",
    }
    body["preview_hash"] = digest(canonical(body))
    return state, body, env_new, md_new


def atomic_replace(path, content, mode):
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    temporary = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode & 0o777,
            dir_fd=directory_fd,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def acquire_lock(factory, name=".envelope.lock"):
    lock = factory / name
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise ControlError("envelope control lock is busy") from exc
    secure_directory(lock)
    return lock


def inspect_command(args):
    state = read_state(Path(args.factory_root))
    values = state[-1]
    result = {
        "env_sha256": digest(state[4]),
        "markdown_sha256": digest(state[5]),
        "roles": {role: effective_role(values, role) for role in ROLES},
        "schema": "factory-envelope-inspect/v1",
        "values": values,
    }
    print(canonical(result).decode())


def plan_command(args):
    _, preview, _, _ = envelope_preview(Path(args.factory_root), changes_from_args(args.set))
    print(canonical(preview).decode())


def apply_command(args):
    if not HASH.fullmatch(args.approve_hash):
        raise ControlError("--approve-hash must be a lowercase SHA-256")
    root = Path(args.factory_root)
    initial = read_state(root)
    launch_lock = acquire_lock(initial[1], ".launch.lock")
    lock = None
    try:
        lock = acquire_lock(initial[1])
        state, preview, env_new, md_new = envelope_preview(root, changes_from_args(args.set))
        if preview["preview_hash"] != args.approve_hash:
            raise ControlError("approval hash does not match the exact current preview")
        env_mode = secure_file(state[2]).st_mode
        md_mode = secure_file(state[3]).st_mode
        # Publish documentation first. If the second write fails, restore it;
        # under the lock readers either see the old pair or the complete pair.
        atomic_replace(state[3], md_new, md_mode)
        try:
            atomic_replace(state[2], env_new, env_mode)
        except Exception:
            atomic_replace(state[3], state[5], md_mode)
            raise
        result = dict(preview)
        result["status"] = "applied"
        print(canonical(result).decode())
    finally:
        if lock is not None:
            lock.rmdir()
        launch_lock.rmdir()


def override_directory(factory):
    return secure_directory(factory / "envelope-overrides", create=True)


def override_changes(scope, changes):
    allowed = {
        "next-attempt": set(BASE_KEYS + ROLE_KEYS),
        "ticket": set(BASE_KEYS + ROLE_KEYS),
        "role": set(BASE_KEYS + ROLE_KEYS),
        "product-day": {"DAILY_CAP_USD"},
        "global-day": {"GLOBAL_DAILY_CAP_USD"},
    }[scope]
    if not set(changes) <= allowed:
        raise ControlError(f"{scope} override contains an unsupported limit")
    local = {key: value for key, value in changes.items() if key in ALL_KEYS}
    validate_values(local)
    if "GLOBAL_DAILY_CAP_USD" in changes:
        positive_money("GLOBAL_DAILY_CAP_USD", changes["GLOBAL_DAILY_CAP_USD"])


def override_preview(args):
    state = read_state(Path(args.factory_root))
    changes = {}
    for item in args.set:
        if "=" not in item:
            raise ControlError("--set requires KEY=value")
        key, value = item.split("=", 1)
        if key in changes:
            raise ControlError(f"duplicate change for {key}")
        changes[key] = value
    if not changes:
        raise ControlError("at least one --set is required")
    override_changes(args.scope, changes)
    if args.ticket and not TICKET.fullmatch(args.ticket):
        raise ControlError("ticket must match T-NNN")
    if args.role and args.role not in ROLES:
        raise ControlError("override role is invalid")
    if args.scope == "ticket" and not args.ticket:
        raise ControlError("ticket scope requires --ticket")
    if args.scope == "role" and not args.role:
        raise ControlError("role scope requires --role")
    if args.scope == "next-attempt" and not (args.ticket or args.role):
        raise ControlError("next-attempt scope requires --ticket and/or --role")
    day = args.day
    if args.scope.endswith("-day"):
        if not day or not UTC_DATE.fullmatch(day):
            raise ControlError("day scope requires --day YYYY-MM-DD")
    elif day:
        raise ControlError("--day is only valid for day scopes")
    body = {
        "base_env_sha256": digest(state[4]),
        "changes": changes,
        "day": day or None,
        "role": args.role or None,
        "schema": "factory-envelope-override/v1",
        "scope": args.scope,
        "ticket": args.ticket or None,
    }
    if args.scope == "global-day":
        if not args.global_env or not Path(args.global_env).is_absolute():
            raise ControlError("global-day scope requires an absolute --global-env")
        global_path = Path(args.global_env)
        body["global_env_sha256"] = digest(secure_read(global_path, 1_000_000))
    record_id = digest(canonical(body))
    preview = {
        "record": body,
        "record_id": record_id,
        "schema": "factory-envelope-override-preview/v1",
    }
    preview["preview_hash"] = digest(canonical(preview))
    return state, preview


def override_plan_command(args):
    _, preview = override_preview(args)
    print(canonical(preview).decode())


def exclusive_record(path, content):
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    descriptor = -1
    try:
        descriptor = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.fsync(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_fd)


def override_apply_command(args):
    if not HASH.fullmatch(args.approve_hash):
        raise ControlError("--approve-hash must be a lowercase SHA-256")
    state, preview = override_preview(args)
    launch_lock = acquire_lock(state[1], ".launch.lock")
    try:
        # CAS includes the permanent envelope digest. Recompute after taking the
        # same lock used by role launch so an old preview cannot be published.
        state, preview = override_preview(args)
        if preview["preview_hash"] != args.approve_hash:
            raise ControlError("approval hash does not match the exact override preview")
        if args.scope == "global-day":
            directory = secure_directory(
                secure_directory(Path(args.global_env).parent) / "envelope-overrides",
                create=True,
            )
        else:
            directory = override_directory(state[1])
        path = directory / f"{preview['record_id']}.json"
        if path.exists() or path.is_symlink():
            secure_file(path)
            if secure_read(path, 16_384).rstrip(b"\n") != canonical(preview["record"]):
                raise ControlError("override record identifier collision")
        else:
            exclusive_record(path, canonical(preview["record"]))
        result = dict(preview)
        result["status"] = "applied"
        print(canonical(result).decode())
    finally:
        launch_lock.rmdir()


def load_override_records(base, ticket, role, day, scopes=None):
    directory = base / "envelope-overrides"
    if not directory.exists():
        return [], {}
    secure_directory(directory)
    consumption = base / "envelope-override-consumptions"
    consumed = set()
    if consumption.exists():
        secure_directory(consumption)
        for path in consumption.glob("*.json"):
            secure_file(path)
            value = json.loads(secure_read(path, 16_384))
            consumed.add(value["record_id"])
    records = []
    changes = {}
    for path in sorted(directory.glob("*.json")):
        info = secure_file(path)
        if info.st_size > 16_384:
            raise ControlError("override record is oversized")
        raw = secure_read(path, 16_384).rstrip(b"\n")
        value = json.loads(raw)
        if value.get("schema") != "factory-envelope-override/v1" or digest(canonical(value)) != path.stem:
            raise ControlError("override record identity is invalid")
        scope = value["scope"]
        if scopes is not None and scope not in scopes:
            continue
        applies = (
            (scope == "next-attempt" and path.stem not in consumed
             and (not value["ticket"] or value["ticket"] == ticket)
             and (not value["role"] or value["role"] == role))
            or (scope == "ticket" and value["ticket"] == ticket)
            or (scope == "role" and value["role"] == role)
            or (scope in {"product-day", "global-day"} and value["day"] == day)
        )
        if applies:
            # Stable filename order gives deterministic precedence; conflicting
            # active records are rejected instead of silently choosing one.
            overlap = set(changes) & set(value["changes"])
            if overlap:
                raise ControlError(f"active override conflict for {sorted(overlap)[0]}")
            changes.update(value["changes"])
            records.append((path.stem, scope))
    return records, changes


def effective_command(args):
    state = read_runtime_state(Path(args.factory_root))
    if not TICKET.fullmatch(args.ticket) or args.role not in ROLES or not UTC_DATE.fullmatch(args.day):
        raise ControlError("effective context is invalid")
    product_scopes = {"next-attempt", "ticket", "role", "product-day"}
    records, changes = load_override_records(
        state[1], args.ticket, args.role, args.day, product_scopes
    )
    if args.global_env and Path(args.global_env).exists():
        global_path = Path(args.global_env)
        if not global_path.is_absolute():
            raise ControlError("global env path must be absolute")
        secure_read(global_path, 1_000_000)
        global_records, global_changes = load_override_records(
            secure_directory(global_path.parent), args.ticket, args.role, args.day,
            {"global-day"},
        )
        overlap = set(changes) & set(global_changes)
        if overlap:
            raise ControlError(f"active override conflict for {sorted(overlap)[0]}")
        records.extend(global_records)
        changes.update(global_changes)
    values = dict(state[-1])
    values.update({key: value for key, value in changes.items() if key in ALL_KEYS})
    validate_values(values)
    effective = effective_role(values, args.role)
    if "GLOBAL_DAILY_CAP_USD" in changes:
        effective["GLOBAL_DAILY_CAP_USD"] = changes["GLOBAL_DAILY_CAP_USD"]
    effective["FACTORY_ENVELOPE_OVERRIDE_IDS"] = ",".join(record_id for record_id, _ in records)
    effective["FACTORY_ENVELOPE_NEXT_OVERRIDE_IDS"] = ",".join(
        record_id for record_id, scope in records if scope == "next-attempt"
    )
    if args.format == "json":
        print(canonical({"effective": effective, "schema": "factory-envelope-effective/v1"}).decode())
    else:
        for key in BASE_KEYS + ("GLOBAL_DAILY_CAP_USD", "FACTORY_ENVELOPE_OVERRIDE_IDS",
                                "FACTORY_ENVELOPE_NEXT_OVERRIDE_IDS"):
            if key in effective:
                print(f"{key}={effective[key]}")


def consume_command(args):
    state = read_runtime_state(Path(args.factory_root))
    if not RUN_ID.fullmatch(args.run_id):
        raise ControlError("run identifier is invalid")
    ids = [item for item in args.record_ids.split(",") if item]
    if any(not HASH.fullmatch(item) for item in ids):
        raise ControlError("override record identifier is invalid")
    if not ids:
        print(canonical({"consumed": [], "schema": "factory-envelope-consumption/v1"}).decode())
        return
    source = override_directory(state[1])
    target = secure_directory(state[1] / "envelope-override-consumptions", create=True)
    consumed = []
    for record_id in ids:
        record_path = source / f"{record_id}.json"
        secure_file(record_path)
        value = json.loads(secure_read(record_path, 16_384))
        if value.get("scope") != "next-attempt" or digest(canonical(value)) != record_id:
            raise ControlError("only an exact next-attempt record may be consumed")
        receipt = {"record_id": record_id, "run_id": args.run_id,
                   "schema": "factory-envelope-override-consumption/v1"}
        receipt_path = target / f"{record_id}.json"
        if receipt_path.exists() or receipt_path.is_symlink():
            secure_file(receipt_path)
            existing = json.loads(secure_read(receipt_path, 16_384))
            if existing != receipt:
                raise ControlError("next-attempt override was consumed by another run")
        else:
            exclusive_record(receipt_path, canonical(receipt))
        consumed.append(record_id)
    print(canonical({"consumed": consumed, "schema": "factory-envelope-consumption/v1"}).decode())


def parser():
    result = argparse.ArgumentParser()
    commands = result.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--factory-root", required=True)
    inspect.set_defaults(handler=inspect_command)
    for name, handler in (("plan", plan_command), ("apply", apply_command)):
        command = commands.add_parser(name)
        command.add_argument("--factory-root", required=True)
        command.add_argument("--set", action="append", default=[])
        if name == "apply":
            command.add_argument("--approve-hash", required=True)
        command.set_defaults(handler=handler)
    for name, handler in (("override-plan", override_plan_command),
                          ("override-apply", override_apply_command)):
        command = commands.add_parser(name)
        command.add_argument("--factory-root", required=True)
        command.add_argument("--scope", required=True,
                             choices=("next-attempt", "ticket", "role", "product-day", "global-day"))
        command.add_argument("--ticket")
        command.add_argument("--role")
        command.add_argument("--day")
        command.add_argument("--global-env")
        command.add_argument("--set", action="append", default=[])
        if name == "override-apply":
            command.add_argument("--approve-hash", required=True)
        command.set_defaults(handler=handler)
    effective = commands.add_parser("effective")
    effective.add_argument("--factory-root", required=True)
    effective.add_argument("--ticket", required=True)
    effective.add_argument("--role", required=True)
    effective.add_argument("--day", required=True)
    effective.add_argument("--global-env")
    effective.add_argument("--format", choices=("json", "shell"), default="json")
    effective.set_defaults(handler=effective_command)
    consume = commands.add_parser("consume")
    consume.add_argument("--factory-root", required=True)
    consume.add_argument("--record-ids", required=True)
    consume.add_argument("--run-id", required=True)
    consume.set_defaults(handler=consume_command)
    return result


def main():
    try:
        args = parser().parse_args()
        args.handler(args)
    except (ControlError, OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
